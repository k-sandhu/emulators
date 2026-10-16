"""Constructors for new store records (channels, ids) shared by the seed and the API."""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.slack import access


def new_id(ctx: ProviderContext, prefix: str, collection: str, length: int = 10) -> str:
    """A fresh Slack-style id (``C`` + 10 uppercase alphanumerics) not yet used in ``collection``."""
    while True:
        candidate = prefix + ctx.ids.base62(length).upper()
        if candidate not in ctx.store.collection(collection):
            return candidate


def make_channel(
    ctx: ProviderContext,
    *,
    channel_id: str,
    name: str = "",
    creator: str,
    created: int,
    members: list[str],
    is_private: bool = False,
    is_im: bool = False,
    is_mpim: bool = False,
    is_general: bool = False,
    topic: str = "",
    purpose: str = "",
    ext_team_ids: list[str] | None = None,
) -> dict[str, Any]:
    team = access.team_id(ctx)
    private = is_private or is_mpim
    shared = list(ext_team_ids or [])
    return {
        "id": channel_id,
        "name": name,
        "is_channel": not private and not is_im,
        "is_group": private,
        "is_im": is_im,
        "is_mpim": is_mpim,
        "is_private": private,
        "created": created,
        "is_archived": False,
        "is_general": is_general,
        "name_normalized": name.lower(),
        "is_shared": bool(shared),
        "is_ext_shared": bool(shared),
        "shared_team_ids": [team, *shared],
        "creator": creator,
        "updated": created * 1000,
        "topic": {"value": topic, "creator": creator if topic else "", "last_set": created if topic else 0},
        "purpose": {"value": purpose, "creator": creator if purpose else "", "last_set": created if purpose else 0},
        "previous_names": [],
        "_members": list(dict.fromkeys(members)),
        "_marks": {},
        "_last_ts": "0.000000",
        "_closed_for": [],
    }
