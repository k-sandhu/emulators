"""Batch variants (create_folder, delete, move, copy) and the async-job polling routes.

Dropbox answers batch calls with ``async_job_id`` and clients poll ``*/check``. The emulator runs
the work eagerly and keeps the result in the ``jobs`` collection; ``in_progress`` is returned for
the first ``ASYNC_JOB_POLLS`` polls (default 0) so retry loops can be exercised on demand.
"""

from __future__ import annotations

from typing import Any

from emulators.core.errors import EmulatorError
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import Args, api_error, tag
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.files_core import (
    create_folder_at,
    delete_at,
    relocate_path,
)

BATCH_MAX = 10000


def job_id(c: m.Call) -> str:
    return "dbjid:AA" + m.new_token(c.ctx, "job", 38)


def error_of(exc: EmulatorError) -> dict[str, Any]:
    body = exc.body
    return body["error"] if isinstance(body, dict) and "error" in body else tag("other")


def launch(c: m.Call, kind: str, result: dict[str, Any], status: str = "complete") -> dict[str, Any]:
    jid = job_id(c)
    polls = int(c.ctx.setting("ASYNC_JOB_POLLS", "0") or 0)
    c.ctx.store.collection(m.JOBS).put(
        {"id": jid, "kind": kind, "status": status, "result": result, "polls": polls, "account": c.actor.id}
    )
    return tag("async_job_id", async_job_id=jid)


def poll(c: m.Call, kind: str) -> dict[str, Any]:
    job = c.ctx.store.collection(m.JOBS).get(c.args.str("async_job_id"))
    if job is None or job["kind"] != kind:
        raise api_error(tag("invalid_async_job_id"))
    if job["polls"] > 0:
        job["polls"] -= 1
        return tag("in_progress")
    if job["status"] == "failed":
        return tag("failed", failed=job["result"])
    return tag("complete", **job["result"])


def entries_of(c: m.Call) -> list[Args]:
    raw = c.args.list("entries")
    if len(raw) > BATCH_MAX:
        raise api_error(tag("too_many_files"))
    return [Args(item, "request body: entries") for item in raw]


# -- create_folder_batch ---------------------------------------------------------------
@route("files/create_folder_batch", scope="files.content.write")
def create_folder_batch(c: m.Call) -> Any:
    auto = c.args.bool("autorename")
    out: list[dict[str, Any]] = []
    for path in c.args.list("paths"):
        try:
            rec = create_folder_at(c, path, auto)
            out.append(tag("success", metadata=m.leaf(m.folder_metadata(c, rec))))
        except EmulatorError as exc:
            out.append(tag("failure", failure=error_of(exc)))
    result = {"entries": out}
    if c.args.bool("force_async"):
        return launch(c, "create_folder", result)
    return tag("complete", **result)


@route("files/create_folder_batch/check", scope="files.content.write")
def create_folder_batch_check(c: m.Call) -> Any:
    return poll(c, "create_folder")


# -- delete_batch ----------------------------------------------------------------------
@route("files/delete_batch", scope="files.content.write")
def delete_batch(c: m.Call) -> Any:
    out: list[dict[str, Any]] = []
    for entry in entries_of(c):
        try:
            meta = delete_at(c, entry.str("path"), entry.str("parent_rev", None))
            out.append(tag("success", metadata=meta))
        except EmulatorError as exc:
            out.append(tag("failure", failure=error_of(exc)))
    return launch(c, "delete", {"entries": out})


@route("files/delete_batch/check", scope="files.content.write")
def delete_batch_check(c: m.Call) -> Any:
    return poll(c, "delete")


# -- move / copy batches ---------------------------------------------------------------
def relocation_batch_v2(c: m.Call, copy: bool) -> Any:
    auto = c.args.bool("autorename")
    out: list[dict[str, Any]] = []
    for entry in entries_of(c):
        try:
            meta = relocate_path(c, entry.str("from_path"), entry.str("to_path"), copy=copy, autorename_=auto)
            out.append(tag("success", success=meta))
        except EmulatorError as exc:
            out.append(tag("failure", failure=tag("relocation_error", relocation_error=error_of(exc))))
    return launch(c, "relocation_v2", {"entries": out})


def relocation_batch_v1(c: m.Call, copy: bool) -> Any:
    auto = c.args.bool("autorename")
    out: list[dict[str, Any]] = []
    for entry in entries_of(c):
        try:
            meta = relocate_path(c, entry.str("from_path"), entry.str("to_path"), copy=copy, autorename_=auto)
        except EmulatorError as exc:
            return launch(c, "relocation", error_of(exc), status="failed")
        out.append({"metadata": meta})
    return launch(c, "relocation", {"entries": out})


@route("files/copy_batch_v2", scope="files.content.write")
def copy_batch_v2(c: m.Call) -> Any:
    return relocation_batch_v2(c, True)


@route("files/move_batch_v2", scope="files.content.write")
def move_batch_v2(c: m.Call) -> Any:
    return relocation_batch_v2(c, False)


@route("files/copy_batch/check_v2", scope="files.content.write")
def copy_batch_check_v2(c: m.Call) -> Any:
    return poll(c, "relocation_v2")


@route("files/move_batch/check_v2", scope="files.content.write")
def move_batch_check_v2(c: m.Call) -> Any:
    return poll(c, "relocation_v2")


@route("files/copy_batch", scope="files.content.write")
def copy_batch(c: m.Call) -> Any:
    return relocation_batch_v1(c, True)


@route("files/move_batch", scope="files.content.write")
def move_batch(c: m.Call) -> Any:
    return relocation_batch_v1(c, False)


@route("files/copy_batch/check", scope="files.content.write")
def copy_batch_check(c: m.Call) -> Any:
    return poll(c, "relocation")


@route("files/move_batch/check", scope="files.content.write")
def move_batch_check(c: m.Call) -> Any:
    return poll(c, "relocation")
