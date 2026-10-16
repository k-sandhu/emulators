"""In-memory state for one provider: named collections of JSON-like records plus blobs.

Records are plain dicts in the provider's own wire shape (camelCase for Graph, snake_case
for Stripe...), so routes can usually return them with little or no mapping. The whole
store can be snapshotted and restored, which is how ``reset`` stays fast: the seeded
state is captured once and restored by deep copy.

Blob content (file bytes, recordings) is content-addressed and immutable, so snapshots
share blobs by reference instead of copying megabytes around.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

Record = dict[str, Any]


class Collection:
    """An insertion-ordered ``id -> record`` map with small query helpers."""

    def __init__(self, name: str, items: dict[str, Record] | None = None) -> None:
        self.name = name
        self._items: dict[str, Record] = items if items is not None else {}

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Record]:
        return iter(list(self._items.values()))

    def __contains__(self, record_id: object) -> bool:
        return record_id in self._items

    def get(self, record_id: str | None) -> Record | None:
        if record_id is None:
            return None
        return self._items.get(str(record_id))

    def put(self, record: Record, *, key: str = "id") -> Record:
        """Insert or replace ``record`` under ``record[key]``."""
        self._items[str(record[key])] = record
        return record

    def put_as(self, record_id: str, record: Record) -> Record:
        self._items[str(record_id)] = record
        return record

    def update(self, record_id: str, **fields: Any) -> Record:
        record = self._items[str(record_id)]
        record.update(fields)
        return record

    def delete(self, record_id: str) -> Record | None:
        return self._items.pop(str(record_id), None)

    def ids(self) -> list[str]:
        return list(self._items)

    def values(self) -> list[Record]:
        return list(self._items.values())

    def filter(self, predicate: Callable[[Record], bool]) -> list[Record]:
        return [r for r in self._items.values() if predicate(r)]

    def where(self, **equals: Any) -> list[Record]:
        return [r for r in self._items.values() if all(r.get(k) == v for k, v in equals.items())]

    def first(self, **equals: Any) -> Record | None:
        for record in self._items.values():
            if all(record.get(k) == v for k, v in equals.items()):
                return record
        return None

    def clear(self) -> None:
        self._items.clear()


class BlobStore:
    """Content-addressed byte storage (sha256 hex keys)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes) -> str:
        key = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._blobs.setdefault(key, bytes(data))
        return key

    def get(self, key: str) -> bytes:
        return self._blobs[key]

    def has(self, key: str) -> bool:
        return key in self._blobs

    def size(self, key: str) -> int:
        return len(self._blobs[key])

    def __len__(self) -> int:
        return len(self._blobs)


class Store:
    """All mutable state of one provider."""

    def __init__(self, blobs: BlobStore | None = None) -> None:
        self._collections: dict[str, Collection] = {}
        self.blobs = blobs or BlobStore()
        # Free-form provider scalars (cursors, counters, settings mutated at runtime).
        self.meta: dict[str, Any] = {}
        self.lock = threading.RLock()

    def collection(self, name: str) -> Collection:
        existing = self._collections.get(name)
        if existing is None:
            existing = self._collections[name] = Collection(name)
        return existing

    __getitem__ = collection

    def names(self) -> list[str]:
        return sorted(self._collections)

    def clear(self) -> None:
        self._collections.clear()
        self.meta.clear()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "collections": {name: copy.deepcopy(c._items) for name, c in self._collections.items()},
                "meta": copy.deepcopy(self.meta),
            }

    def restore(self, snapshot: dict[str, Any]) -> None:
        with self.lock:
            self._collections = {
                name: Collection(name, copy.deepcopy(items))
                for name, items in snapshot["collections"].items()
            }
            self.meta = copy.deepcopy(snapshot["meta"])

    def dump(self, *, include: Iterable[str] | None = None) -> dict[str, list[Record]]:
        wanted = set(include) if include else None
        return {
            name: c.values()
            for name, c in sorted(self._collections.items())
            if wanted is None or name in wanted
        }

    def save(self, path: Path) -> None:
        """Persist collections (not blobs) as JSON — for debugging and fixtures."""
        path.write_text(json.dumps(self.snapshot(), default=str, indent=1), encoding="utf-8")
