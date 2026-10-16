"""Official SDK regressions for issues found during the adversarial fidelity audit."""

from __future__ import annotations

from typing import Any

import pytest

from tests.sdk.test_dropbox_sdk import client

pytestmark = pytest.mark.sdk
dropbox = pytest.importorskip("dropbox")
from dropbox import exceptions, files  # noqa: E402


def test_folder_id_descendants_and_idempotent_upload(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    folder = dbx.files_create_folder_v2("/sdk-audit/nested").metadata
    original = dbx.files_upload(b"same bytes", "/sdk-audit/nested/child.txt")
    by_id = dbx.files_get_metadata(folder.id + "/child.txt")
    assert by_id.id == original.id
    downloaded, response = dbx.files_download(folder.id + "/child.txt")
    assert downloaded.id == original.id and response.content == b"same bytes"
    again = dbx.files_upload(b"same bytes", "/sdk-audit/nested/child.txt")
    assert again.rev == original.rev
    dbx.files_upload(b"changed", original.id, mode=files.WriteMode.overwrite)
    with pytest.raises(exceptions.ApiError) as conflict:
        dbx.files_upload(b"stale", original.id, mode=files.WriteMode.update(original.rev))
    assert conflict.value.error.get_path().reason.get_conflict().is_file()
    copy = dbx.files_upload(b"stale", original.id, mode=files.WriteMode.update(original.rev), autorename=True)
    assert copy.id != original.id and "conflicted copy" in copy.name


def test_open_batch_session_reports_not_closed_and_can_retry(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    session = dbx.files_upload_session_start(b"data")
    cursor = files.UploadSessionCursor(session.session_id, 4)
    entry = files.UploadSessionFinishArg(cursor, files.CommitInfo("/sdk-audit/batch.txt"))
    failed = dbx.files_upload_session_finish_batch_v2([entry]).entries[0]
    assert failed.is_failure() and failed.get_failure().get_lookup_failed().is_not_closed()
    dbx.files_upload_session_append_v2(b"", cursor, close=True)
    success = dbx.files_upload_session_finish_batch_v2([entry]).entries[0]
    assert success.is_success() and success.get_success().size == 4


def test_shared_folder_link_pagination_decodes_in_sdk(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    for i in range(5):
        dbx.files_upload(str(i).encode(), f"/sdk-audit/shared/f{i}.txt")
    link = dbx.sharing_create_shared_link_with_settings("/sdk-audit/shared")
    first = dbx.files_list_folder("", limit=2, shared_link=files.SharedLink(link.url))
    assert first.has_more and len(first.entries) == 2
    entries = list(first.entries)
    while first.has_more:
        first = dbx.files_list_folder_continue(first.cursor)
        entries += first.entries
    assert [entry.name for entry in entries] == [f"f{i}.txt" for i in range(5)]
