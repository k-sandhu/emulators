"""Regenerate the runtime argument catalog from the official Dropbox SDK (dev-only).

Run ``uv run python tests/providers/dropbox/generate_argument_schema.py`` after updating
the SDK. Runtime code reads the resulting plain JSON, never importing Dropbox or Stone.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

from stone.backends.python_rsrc import stone_validators as sv

from emulators.core.hub import EmulatorHub


def catalog() -> dict[str, Any]:
    EmulatorHub(providers=["dropbox"], anchor="2026-10-16")
    from emulators.providers.dropbox.dispatch import ROUTES

    definitions: dict[str, Any] = {}

    def schema(v: Any) -> dict[str, Any]:
        if isinstance(v, sv.Nullable):
            return {"type": "nullable", "item": schema(v.validator)}
        if isinstance(v, (sv.Struct, sv.Union)):
            cls = v.definition
            name = cls.__module__ + "." + cls.__name__
            if name not in definitions:
                definitions[name] = {}
                if isinstance(v, sv.Struct):
                    empty = cls()
                    definitions[name] = {
                        "type": "struct",
                        "fields": {n: schema(f) for n, f in cls._all_fields_},
                        "required": [n for n, _ in cls._all_fields_ if not hasattr(empty, n)],
                    }
                else:
                    definitions[name] = {
                        "type": "union",
                        "tags": {n: schema(f) for n, f in cls._tagmap.items()},
                    }
            return {"ref": name}
        if isinstance(v, sv.List):
            return {
                "type": "list",
                "item": schema(v.item_validator),
                "min": v.min_items,
                "max": v.max_items,
            }
        if isinstance(v, sv.String):
            return {"type": "string", "min": v.min_length, "max": v.max_length, "pattern": v.pattern}
        if isinstance(v, sv.Integer):
            return {"type": "integer", "min": v.minimum, "max": v.maximum}
        if isinstance(v, (sv.Float32, sv.Float64)):
            return {"type": "float"}
        if isinstance(v, sv.Timestamp):
            return {"type": "timestamp", "format": v.format}
        if isinstance(v, sv.Boolean):
            return {"type": "boolean"}
        if isinstance(v, sv.Void):
            return {"type": "void"}
        raise TypeError(type(v))

    routes: dict[str, Any] = {}
    for name in sorted(ROUTES):
        namespace, _, rest = name.partition("/")
        module = importlib.import_module("dropbox." + namespace)
        sdk_route = getattr(module, rest.replace("/", "_"), None)
        if sdk_route is not None:
            routes[name] = schema(sdk_route.arg_type)
    return {"sdk_version": version("dropbox"), "routes": routes, "definitions": definitions}


if __name__ == "__main__":
    data = catalog()
    destination = Path("src/emulators/providers/dropbox/argument_schema.json")
    # One named type per line keeps the generated catalog small and diffs readable.
    destination.write_text(
        "{\n"
        + ",\n".join(
            json.dumps(key)
            + ": "
            + (
                "{\n"
                + ",\n".join(
                    json.dumps(k) + ": " + json.dumps(v, sort_keys=True) for k, v in sorted(value.items())
                )
                + "\n}"
                if isinstance(value, dict)
                else json.dumps(value)
            )
            for key, value in data.items()
        )
        + "\n}\n",
        encoding="utf-8",
    )
