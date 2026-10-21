"""A failed upload must leave the recovery session promised by Dropbox's wire error."""

from __future__ import annotations

from typing import Any


async def test_failed_direct_upload_can_commit_its_recovery_session(dbx: Any) -> None:
    token = await dbx.token()
    await dbx.put(token, "/conflict.txt", b"original")
    failed = await dbx.upload(token, "files/upload", {"path": "/conflict.txt"}, b"recover these bytes")
    assert failed.status_code == 409
    session_id = failed.json()["error"]["upload_session_id"]
    recovered = await dbx.upload(
        token,
        "files/upload_session/finish",
        {
            "cursor": {"session_id": session_id, "offset": len(b"recover these bytes")},
            "commit": {"path": "/recovered.txt"},
        },
        b"",
    )
    assert recovered.status_code == 200, recovered.text
    downloaded, _ = await dbx.download(token, "/recovered.txt")
    assert downloaded.content == b"recover these bytes"
    reused = await dbx.upload(
        token,
        "files/upload_session/finish",
        {
            "cursor": {"session_id": session_id, "offset": len(b"recover these bytes")},
            "commit": {"path": "/again.txt"},
        },
        b"",
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["lookup_failed"][".tag"] == "not_found"
