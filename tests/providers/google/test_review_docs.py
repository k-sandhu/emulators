"""Docs batches must reject ambiguous edits without changing the document."""

from __future__ import annotations

from typing import Any

import pytest

from emulators.testing.helpers import bearer, mint

BASE = "https://docs.googleapis.com/v1/documents"


@pytest.mark.parametrize(
    "invalid",
    [
        {"insertText": {"text": "bad"}},
        {"insertText": {"text": "bad", "location": {}}},
        {"insertText": {"text": "bad", "location": {"index": 1}, "endOfSegmentLocation": {}}},
        {
            "insertText": {"text": "bad", "location": {"index": 1}},
            "replaceAllText": {"containsText": {"text": "x"}, "replaceText": "y"},
        },
        {"insertText": {"text": "bad", "endOfSegmentLocation": {"segmentId": "missing"}}},
    ],
)
async def test_invalid_docs_batch_does_not_apply_valid_prefix(
    make_client: Any, invalid: dict[str, Any]
) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        doc = (await client.post(BASE, headers=auth, json={"title": "Atomic document"})).json()
        path = BASE + "/" + doc["documentId"]
        before = (await client.get(path, headers=auth)).json()
        response = await client.post(
            path + ":batchUpdate",
            headers=auth,
            json={"requests": [{"insertText": {"text": "prefix", "location": {"index": 1}}}, invalid]},
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
        assert (await client.get(path, headers=auth)).json() == before


async def test_insert_text_strips_vendor_documented_control_characters(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        doc = (await client.post(BASE, headers=auth, json={"title": "Text normalization"})).json()
        path = BASE + "/" + doc["documentId"]
        response = await client.post(
            path + ":batchUpdate",
            headers=auth,
            json={
                "requests": [
                    {
                        "insertText": {
                            "text": "A\u0000\u0008\u000c\u001f\ue000B😀\t\nC",
                            "endOfSegmentLocation": {},
                        }
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        content = (await client.get(path, headers=auth)).json()["body"]["content"]
        text = "".join(
            e["textRun"]["content"] for p in content if "paragraph" in p for e in p["paragraph"]["elements"]
        )
        assert text == "AB😀\t\nC\n"


async def test_docs_write_control_revision_modes_are_mutually_exclusive(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, "https://www.googleapis.com", user="p018"))
        doc = (await client.post(BASE, headers=auth, json={"title": "Revision control"})).json()
        path = BASE + "/" + doc["documentId"]
        response = await client.post(
            path + ":batchUpdate",
            headers=auth,
            json={
                "writeControl": {
                    "requiredRevisionId": doc["revisionId"],
                    "targetRevisionId": doc["revisionId"],
                },
                "requests": [{"insertText": {"text": "rejected", "location": {"index": 1}}}],
            },
        )
        assert response.status_code == 400, response.text
        assert (await client.get(path, headers=auth)).json() == doc
