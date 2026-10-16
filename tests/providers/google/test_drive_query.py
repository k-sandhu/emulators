from __future__ import annotations

from typing import Any

import pytest

from emulators.providers.google.drive_fields import select
from emulators.providers.google.drive_query import compile_query


@pytest.mark.parametrize(
    "query,expected",
    [
        ("name = 'O\\'Brien plan'", True),
        ("name contains 'O'", True),
        ("name contains 'plan'", True),
        ("fullText contains 'budget'", True),
        ("mimeType != 'application/pdf' and trashed = false", True),
        ("'folder1' in parents", True),
        ("'me' in owners", True),
        ("'maya@halcyonadvisory.example' in writers", True),
        ("'reader@halcyonadvisory.example' in readers", True),
        ("not (starred = true or trashed = true)", True),
        ("sharedWithMe", True),
        ("modifiedTime > '2026-01-01T00:00:00'", True),
        ("createdTime <= '2025-01-01T00:00:00Z'", False),
        ("viewedByMeTime > '2026-01-01T00:00:00Z'", False),
        ("visibility = 'anyoneWithLink'", True),
        ("properties has { key='project' and value='northwind' }", True),
        ("appProperties has { key='sync' and value='yes' }", True),
        ("name = 'bad' or name = 'O\\'Brien plan' and trashed = false", True),
    ],
)
def test_query_language(query: Any, expected: Any) -> None:
    record = {
        "name": "O'Brien plan",
        "mimeType": "text/plain",
        "trashed": False,
        "starred": False,
        "parents": ["folder1"],
        "_owners": ["me"],
        "_writers": ["maya@halcyonadvisory.example"],
        "_readers": ["reader@halcyonadvisory.example"],
        "_text": "Annual budget discussion",
        "sharedWithMe": True,
        "modifiedTime": "2026-10-15T00:00:00Z",
        "createdTime": "2026-02-01T00:00:00Z",
        "visibility": "anyoneWithLink",
        "properties": {"project": "northwind"},
        "appProperties": {"sync": "yes"},
    }
    assert compile_query(query)(record) is expected


def test_fields_nested_slashes_wildcards_and_merge() -> None:
    record = {
        "nextPageToken": "token",
        "files": [
            {
                "id": "1",
                "name": "A",
                "permissions": [{"id": "p", "role": "reader"}],
                "capabilities": {"canEdit": True, "canDownload": True},
                "_internal": "secret",
            }
        ],
        "_secret": "secret",
    }
    assert select(record, "nextPageToken,files(id,permissions/id),files/capabilities/*") == {
        "nextPageToken": "token",
        "files": [
            {"id": "1", "permissions": [{"id": "p"}], "capabilities": {"canEdit": True, "canDownload": True}}
        ],
    }
    # Internal storage fields must never leak through wildcard selectors.
    assert "_secret" not in select(record, "*")
