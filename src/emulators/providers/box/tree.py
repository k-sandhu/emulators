"""The folder tree: children, sizes, ancestry and name-uniqueness, built per request.

The store holds flat ``box_folders`` / ``box_files`` collections; a :class:`Tree` indexes them
by parent once (a few hundred records, microseconds) so listings and recursive sizes stay
cheap without keeping a second, mutable index that could drift from the records.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.box import db

Record = dict[str, Any]


class Tree:
    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self._kids: dict[str, list[Record]] = {}
        for collection in (db.folders(ctx), db.files(ctx), ctx.store.collection(db.WEB_LINKS)):
            for record in collection:
                parent = record.get("_parent")
                if parent is not None:
                    self._kids.setdefault(parent, []).append(record)
        self._sizes: dict[str, int] = {}

    def children(self, parent_id: str, *, trashed: bool = False) -> list[Record]:
        """Direct children; trashed items are listed only when asked for."""
        return [r for r in self._kids.get(parent_id, []) if bool(r.get("_trash")) == trashed]

    def all_children(self, parent_id: str) -> list[Record]:
        return list(self._kids.get(parent_id, []))

    def walk(self, folder_id: str, *, include_trashed: bool = False) -> Iterator[Record]:
        """Every descendant (folders and files), parents before children."""
        for child in self._kids.get(folder_id, []):
            if child.get("_trash") and not include_trashed:
                continue
            yield child
            if child["type"] == "folder":
                yield from self.walk(child["id"], include_trashed=include_trashed)

    def size(self, folder_id: str) -> int:
        cached = self._sizes.get(folder_id)
        if cached is not None:
            return cached
        total = 0
        for child in self.children(folder_id):
            if child["type"] == "file":
                total += int(child.get("size", 0))
            elif child["type"] == "folder":
                total += self.size(child["id"])
        self._sizes[folder_id] = total
        return total

    def find_by_name(self, parent_id: str, name: str, *, ignore: str | None = None) -> Record | None:
        wanted = name.lower()
        for child in self.children(parent_id):
            if child["name"].lower() == wanted and child["id"] != ignore:
                return child
        return None

    def taken_names(self, parent_id: str) -> set[str]:
        return {c["name"].lower() for c in self.children(parent_id)}


def parent_of(ctx: ProviderContext, record: Record) -> Record | None:
    parent = record.get("_parent")
    return db.folders(ctx).get(parent) if parent else None


def ancestors(ctx: ProviderContext, record: Record) -> list[Record]:
    """Parents from the immediate one up to and including the owner's root."""
    chain: list[Record] = []
    seen: set[str] = set()
    current = parent_of(ctx, record)
    while current is not None and current["id"] not in seen:
        chain.append(current)
        seen.add(current["id"])
        current = parent_of(ctx, current)
    return chain


def is_trashed(ctx: ProviderContext, record: Record) -> bool:
    """Trashed itself or living under a trashed folder."""
    if record.get("_trash"):
        return True
    return any(a.get("_trash") for a in ancestors(ctx, record))


def is_within(ctx: ProviderContext, record: Record, folder_id: str) -> bool:
    """True when ``record`` is ``folder_id`` or lives anywhere beneath it."""
    if record["type"] == "folder" and record["id"] == folder_id:
        return True
    return any(a["id"] == folder_id for a in ancestors(ctx, record))
