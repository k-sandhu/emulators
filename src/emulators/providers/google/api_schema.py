"""Wire types extracted from Google's official SDK discovery documents.

Only structural types are retained; runtime needs no SDK or network access.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

_DATA = json.loads(files(__package__).joinpath("api_schema.json").read_text(encoding="utf-8"))
SCHEMAS: dict[str, Any] = _DATA["schemas"]
ROUTES: list[tuple[str, str, str | None, str | None]] = [tuple(r) for r in _DATA["routes"]]
