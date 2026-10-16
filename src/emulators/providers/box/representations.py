"""Representations (``X-Rep-Hints``): PDF, extracted text and image renditions of a file."""

from __future__ import annotations

import re
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.box import content
from emulators.world import documents

Record = dict[str, Any]
HINT = re.compile(r"\[([a-z0-9_]+)(?:\?([^\]]*))?\]")
IMAGE_KINDS = ("jpg", "png")
KINDS = ("pdf", "extracted_text", *IMAGE_KINDS)


def parse_hints(header: str | None) -> list[tuple[str, dict[str, str]]]:
    hints = []
    for kind, raw in HINT.findall(header or ""):
        params = dict(part.split("=", 1) for part in (raw or "").split(",") if "=" in part)
        if kind in KINDS:
            hints.append((kind, params))
    return hints


def kind_of(name: str) -> str:
    """``png_1024x1024`` -> ``png``; ``extracted_text`` stays whole."""
    return "extracted_text" if name.startswith("extracted_text") else name.split("_", 1)[0]


def rep_name(kind: str, params: dict[str, str]) -> str:
    if kind in IMAGE_KINDS:
        return f"{kind}_{params.get('dimensions', '1024x1024')}"
    return kind


def entry(record: Record, kind: str, params: dict[str, str], info_url: str, content_url: str) -> Record:
    properties: Record = {}
    if kind in IMAGE_KINDS:
        properties = {"dimensions": params.get("dimensions", "1024x1024"), "paged": "false", "thumb": "false"}
    return {
        "content": {"url_template": content_url + "/{+asset_path}"},
        "info": {"url": info_url},
        "properties": properties,
        "representation": kind,
        "status": {"state": "success"},
    }


def paragraphs_of(ctx: ProviderContext, record: Record) -> tuple[str, list[str]]:
    if record.get("_doc"):
        doc = ctx.world.document(record["_doc"])
        return doc.title, list(doc.paragraphs)
    text = record.get("_text") or ""
    return record["name"], [line for line in text.splitlines() if line.strip()] or ["(no extractable text)"]


def render(ctx: ProviderContext, record: Record, name: str) -> tuple[bytes, str]:
    """Bytes and media type of the representation called ``name`` (``pdf``, ``png_1024x1024``...)."""
    kind = kind_of(name)
    if kind == "pdf":
        if record["name"].lower().endswith(".pdf"):
            return content.file_bytes(ctx, record), "application/pdf"
        title, paragraphs = paragraphs_of(ctx, record)
        return documents.make_pdf(title, paragraphs), "application/pdf"
    if kind == "extracted_text":
        title, paragraphs = paragraphs_of(ctx, record)
        return "\n".join([title, *paragraphs]).encode(), "text/plain"
    size = 256
    match = re.search(r"(\d+)x(\d+)", name)
    if match:
        size = max(16, min(int(match.group(1)), 256))
    return documents.make_png(size, size, seed=int(record["id"]) % 100_000), "image/png"
