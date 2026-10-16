"""Minimal Docs text editing shares Drive storage, ACLs, exports and revisions."""

from __future__ import annotations

import copy
import re
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import google_error, json_object
from emulators.providers.google.drive_access import get_file, principal
from emulators.providers.google.drive_changes import record_change
from emulators.providers.google.drive_fields import select
from emulators.providers.google.drive_mutations import create_file, save_revision
from emulators.providers.google.drive_seed import DOC


def utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def document_shape(row: dict[str, Any]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "endIndex": 1,
            "sectionBreak": {
                "sectionStyle": {
                    "columnSeparatorStyle": "NONE",
                    "contentDirection": "LEFT_TO_RIGHT",
                    "sectionType": "CONTINUOUS",
                }
            },
        }
    ]
    index = 1
    for paragraph in row.get("_paragraphs") or [""]:
        text = paragraph + "\n"
        end = index + utf16_len(text)
        content.append(
            {
                "startIndex": index,
                "endIndex": end,
                "paragraph": {
                    "elements": [
                        {"startIndex": index, "endIndex": end, "textRun": {"content": text, "textStyle": {}}}
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            }
        )
        index = end
    return {
        "documentId": row["id"],
        "title": row["name"],
        "revisionId": row["version"],
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "body": {"content": content},
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.post("/v1/documents")
    async def create(request: Request) -> dict[str, Any]:
        actor = principal(request, ctx)
        data = await json_object(request)
        row = await create_file(ctx, actor, {"name": data.get("title", "Untitled document"), "mimeType": DOC})
        return select(document_shape(row), request.query_params.get("fields"))

    def doc(fid: str, request: Request, *, write: bool = False) -> dict[str, Any]:
        row = get_file(ctx, principal(request, ctx, legacy=False), fid, write=write)
        if row["mimeType"] != DOC:
            raise google_error(
                400, "This operation is not supported for this document", reason="badRequest", legacy=False
            )
        return row

    @app.get("/v1/documents/{fid}")
    async def get(fid: str, request: Request) -> dict[str, Any]:
        return select(document_shape(doc(fid, request)), request.query_params.get("fields"))

    @app.post("/v1/documents/{fid}:batchUpdate")
    async def batch_update(fid: str, request: Request) -> dict[str, Any]:
        row = doc(fid, request, write=True)
        data = await json_object(request)
        control = data.get("writeControl", {})
        if control.get("requiredRevisionId", row["version"]) != row["version"]:
            raise google_error(
                400, "The provided revision ID does not match the current revision ID.", legacy=False
            )
        working = copy.deepcopy(row)
        text = "\n".join(working.get("_paragraphs", [])) + "\n"
        replies: list[dict[str, Any]] = []
        for item in data.get("requests", []):
            if "insertText" in item:
                edit = item["insertText"]
                offset = edit.get("location", {}).get("index", utf16_len(text)) - 1
                encoded = text.encode("utf-16-le")
                if (
                    not isinstance(offset, int)
                    or not 0 <= offset <= utf16_len(text) - 1
                    or not isinstance(edit.get("text"), str)
                ):
                    raise google_error(400, "Invalid insertion index or text", legacy=False)
                try:
                    text = (
                        encoded[: offset * 2] + edit["text"].encode("utf-16-le") + encoded[offset * 2 :]
                    ).decode("utf-16-le")
                except UnicodeDecodeError:
                    raise google_error(400, "Invalid UTF-16 insertion index", legacy=False) from None
                replies.append({})
            elif "replaceAllText" in item:
                edit = item["replaceAllText"]
                contains = edit.get("containsText", {})
                needle = contains.get("text", "")
                if not needle:
                    raise google_error(400, "Missing text to replace", legacy=False)
                pattern = re.compile(
                    re.escape(needle), flags=0 if contains.get("matchCase") else re.IGNORECASE
                )
                text, count = pattern.subn(
                    lambda _, replacement=edit.get("replaceText", ""): replacement, text
                )
                replies.append({"replaceAllText": {"occurrencesChanged": count}})
            else:
                raise google_error(400, "Invalid requests: unsupported request", legacy=False)
        working["_paragraphs"] = text.removesuffix("\n").split("\n")
        working.pop("_exports", None)
        working["version"] = str(int(row["version"]) + 1)
        working["modifiedTime"] = isoformat_z(ctx.clock.now())
        working["_modified_by"] = principal(request, ctx).person_id
        row.update(working)
        save_revision(ctx, row)
        await record_change(ctx, row, changed="content")
        return {
            "documentId": row["id"],
            "replies": replies,
            "writeControl": {"requiredRevisionId": row["version"]},
        }
