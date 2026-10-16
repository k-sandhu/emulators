"""Shared-link creation and updates shared by ``PUT /files/{id}`` and ``PUT /folders/{id}``."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from emulators.providers.box import access, errors
from emulators.providers.box.access import Principal
from emulators.providers.box.util import input_ts

Record = dict[str, Any]

ACCESS_LEVELS = ("open", "company", "collaborators")


def new_hash(seed: str) -> str:
    return hashlib.sha256(f"{seed}/{secrets.token_hex(8)}".encode()).hexdigest()[:32]


def apply_shared_link(p: Principal, record: Record, value: Any, now: str) -> str | None:
    """Create, update or remove the shared link; returns the Box event type to record."""
    existing = record.get("shared_link")
    if value is None:
        if existing is None:
            return None
        p.require(record, access.SHARE)
        record["shared_link"] = None
        return "ITEM_SHARED_UNSHARE"
    if not isinstance(value, dict):
        raise errors.invalid_parameter("shared_link", "'shared_link' must be an object or null")
    p.require(record, access.SHARE)
    level = value.get("access")
    if level is not None and level not in ACCESS_LEVELS:
        raise errors.invalid_parameter("shared_link.access", f"Invalid access level '{level}'")
    if level not in (None, "collaborators") and access.SET_SHARE_ACCESS not in p.caps(record):
        raise errors.forbidden()
    link: Record
    created = existing is None
    if existing is None:
        link = {
            "hash": new_hash(f"{record['type']}/{record['id']}"),
            "access": level or "open",
            "password": None,
            "unshared_at": None,
            "permissions": {"can_download": True, "can_preview": True, "can_edit": False},
            "download_count": 0,
            "preview_count": 0,
            "vanity_name": None,
            "created_at": now,
            "created_by": p.uid,
        }
    else:
        link = existing
        if level is not None:
            link["access"] = level
    if "password" in value:
        password = value["password"]
        if password is not None:
            if not isinstance(password, str) or len(password) < 6:
                raise errors.invalid_parameter(
                    "shared_link.password", "Password must be at least 6 characters"
                )
            if link["access"] != "open":
                raise errors.invalid_parameter(
                    "shared_link.password", "Passwords are only allowed on 'open' links"
                )
        link["password"] = password
    if "unshared_at" in value:
        link["unshared_at"] = (
            input_ts(value["unshared_at"], "shared_link.unshared_at") if value["unshared_at"] else None
        )
    if "vanity_name" in value:
        name = value["vanity_name"]
        if name is not None and (not isinstance(name, str) or len(name) < 12):
            raise errors.invalid_parameter(
                "shared_link.vanity_name", "Vanity names must be at least 12 characters"
            )
        link["vanity_name"] = name
    perms = value.get("permissions")
    if isinstance(perms, dict):
        for key in ("can_download", "can_edit"):
            if key in perms:
                link["permissions"][key] = bool(perms[key])
        if record["type"] == "folder" and link["permissions"].get("can_edit"):
            raise errors.invalid_parameter("shared_link.permissions", "Folders do not support 'can_edit'")
    elif perms is not None:
        raise errors.invalid_parameter("shared_link.permissions", "'permissions' must be an object")
    record["shared_link"] = link
    return "ITEM_SHARED_CREATE" if created else "ITEM_SHARED_UPDATE"
