"""File bytes: rendering seeded documents and storing uploads in the shared blob store."""

from __future__ import annotations

import mimetypes
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.box import db
from emulators.providers.box.util import sha1_hex
from emulators.world import documents

Record = dict[str, Any]


def render_document(ctx: ProviderContext, doc_id: str, *, revision: int = 0, total: int = 1) -> bytes:
    """Bytes of a world document; ``revision`` < ``total`` renders an earlier, shorter draft."""
    doc = ctx.world.document(doc_id)
    paragraphs = list(doc.paragraphs)
    table = [list(row) for row in doc.table] if doc.table else None
    title = doc.title
    if revision and total > 1 and revision < total:
        keep = max(1, (len(paragraphs) * revision) // total)
        paragraphs = [*paragraphs[:keep], f"Draft revision {revision} of {total} - superseded."]
        if table:
            table = table[: max(2, (len(table) * revision) // total)]
    author = ctx.world.person(doc.owner_id).display_name
    data, _mime = documents.render(doc.kind, title, paragraphs, author=author, created=doc.created, table=table)
    return data


def search_text(doc_id_text: tuple[str, ...], title: str, table: Any) -> str:
    parts = [title, *doc_id_text]
    if table:
        for row in table:
            parts.extend(str(cell) for cell in row)
    return "\n".join(parts).lower()


def materialize_version(ctx: ProviderContext, version: Record) -> Record:
    """Fill in the bytes, size and SHA-1 of a seeded historical version on first use."""
    if version.get("sha1"):
        return version
    data = render_document(ctx, version["_doc"], revision=version["number"], total=version["_total"])
    version["_blob"] = ctx.store.blobs.put(data)
    version["size"] = len(data)
    version["sha1"] = sha1_hex(data)
    return version


def version_bytes(ctx: ProviderContext, version: Record) -> bytes:
    materialize_version(ctx, version)
    return ctx.store.blobs.get(version["_blob"])


def file_bytes(ctx: ProviderContext, record: Record, version_id: str | None = None) -> bytes:
    version = db.versions(ctx).get(version_id or record["_version"])
    if version is None:
        return ctx.store.blobs.get(record["_blob"])
    return version_bytes(ctx, version)


def guess_mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in documents.EXTENSION_TO_MIME:
        return documents.EXTENSION_TO_MIME[ext]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
