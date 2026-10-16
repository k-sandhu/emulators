"""Cloud recordings API: list, get, delete/trash, recover, settings.

Path parameters named ``meeting`` accept a meeting id *or* a meeting UUID (single or
double URL-encoded), exactly like Zoom's ``{meetingId}`` on these endpoints, so every route
uses a ``:path`` matcher and decodes the value itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.zoom import files, lookup, simulate
from emulators.providers.zoom import webhooks as hooks
from emulators.providers.zoom.common import (
    E_VALIDATION,
    LiveCollection,
    Need,
    authenticate,
    invalid_field,
    json_body,
    no_permission,
    paginate,
    query_bool,
    query_int,
    recording_missing,
    respond,
    zoom_error,
)
from emulators.providers.zoom.shapes import share_token
from emulators.providers.zoom.timeutil import day_range, in_days

LIST_USER_RECORDINGS = Need("recording:read", "cloud_recording:read:list_user_recordings")
LIST_ACCOUNT_RECORDINGS = Need(
    "recording:read", "cloud_recording:read:list_account_recordings", admin_only=True
)
READ_RECORDING = Need("recording:read", "cloud_recording:read:list_recording_files")
DELETE_RECORDING = Need("recording:write", "cloud_recording:delete:meeting_recording")
DELETE_FILE = Need("recording:write", "cloud_recording:delete:recording_file")
RECOVER = Need("recording:write", "cloud_recording:update:recover_meeting_recordings")
READ_SETTINGS = Need("recording:read", "cloud_recording:read:recording_settings")
WRITE_SETTINGS = Need("recording:write", "cloud_recording:update:recording_settings")
RECOVER_FILE = Need("recording:write", "cloud_recording:update:recover_single_recording")
MAX_TTL_S = 604800
SHARE_VALUES = ("publicly", "internally", "none")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    recordings = LiveCollection(ctx, "recordings")

    def listing(request: Request, rows: list[dict[str, Any]], *, account: bool = False) -> dict[str, Any]:
        q = request.query_params
        today = ctx.clock.now().date()
        trash = query_bool(request, "trash")
        lo, hi = day_range(
            None if trash else q.get("from"),
            None if trash else q.get("to"),
            default_from=today,
            default_to=today + timedelta(days=1),
        )
        file_level = trash and q.get("trash_type") == "recording_file"
        if q.get("trash_type") not in (None, "", "meeting_recordings", "recording_file"):
            raise invalid_field("trash_type")
        keep = []
        for rec in rows:
            if not trash and not in_days(rec["start_time"], lo, hi):
                continue
            if q.get("meeting_id") and str(rec["meeting_id"]) != q["meeting_id"].replace(" ", ""):
                continue
            if trash:
                trashed_files = [f for f in rec["recording_files"] if f.get("_trashed")]
                if (file_level and not trashed_files) or (not file_level and not rec["trashed"]):
                    continue
            elif rec["trashed"] or not files.live_files(rec):
                continue
            keep.append(rec)
        keep.sort(key=lambda r: (r["start_time"], r["id"]), reverse=True)
        page, meta = paginate(request, keep)
        return {
            "from": lo.isoformat(),
            "to": hi.isoformat(),
            "page_count": meta["page_count"],
            "page_size": meta["page_size"],
            "total_records": meta["total_records"],
            "next_page_token": meta["next_page_token"],
            "meetings": [files.recording_wire(r, trash_view=trash) for r in page],
        }

    @app.get("/v2/users/{user_id}/recordings")
    async def user_recordings(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(LIST_USER_RECORDINGS)
        user = lookup.acting_user(ctx, principal, user_id, admin_level=admin)
        rows = [r for r in recordings if r["host_id"] == user["id"]]
        return respond(request, ctx, listing(request, rows))

    @app.get("/v2/accounts/{account_id}/recordings")
    async def account_recordings(request: Request, account_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(LIST_ACCOUNT_RECORDINGS):
            raise no_permission()
        if account_id not in ("me", ctx.world.org.zoom_account_id):
            raise zoom_error(404, 2001, f"Account does not exist: {account_id}.")
        return respond(request, ctx, listing(request, recordings.values(), account=True))

    # -- one meeting's recordings ---------------------------------------------------------

    def settings_of(rec: dict[str, Any]) -> dict[str, Any]:
        settings = rec.setdefault("settings", dict(files.RECORDING_SETTINGS))
        return {**settings, "topic": rec["topic"]}

    @app.get("/v2/meetings/{meeting:path}/recordings/settings")
    async def get_recording_settings(request: Request, meeting: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(READ_SETTINGS)
        rec = lookup.find_recording(ctx, meeting)
        lookup.ensure_recording_access(ctx, principal, admin, rec)
        return respond(request, ctx, settings_of(rec))

    @app.patch("/v2/meetings/{meeting:path}/recordings/settings")
    async def patch_recording_settings(request: Request, meeting: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(WRITE_SETTINGS)
        rec = lookup.find_recording(ctx, meeting)
        lookup.ensure_recording_access(ctx, principal, admin, rec)
        data = await json_body(request)
        if data.get("share_recording") not in (None, *SHARE_VALUES):
            raise invalid_field("share_recording")
        if data.get("approval_type") not in (None, 0, 1, 2):
            raise invalid_field("approval_type")
        current = rec.setdefault("settings", dict(files.RECORDING_SETTINGS))
        for key in files.RECORDING_SETTINGS:
            if key in data:
                current[key] = data[key]
        if "password" in data:
            rec["password"] = data["password"]
            rec["recording_play_passcode"] = share_token(
                ctx.world, "passcode", rec["id"], data["password"], length=40
            )
        if "topic" in data:
            rec["topic"] = data["topic"]
        return Response(status_code=204)

    @app.put("/v2/meetings/{meeting:path}/recordings/status")
    async def recover_recordings(request: Request, meeting: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(RECOVER)
        rec = lookup.find_recording(ctx, meeting, include_trashed=True)
        lookup.ensure_recording_access(ctx, principal, admin, rec)
        if (await json_body(request)).get("action") != "recover":
            raise invalid_field("action")
        if not rec["trashed"] and not any(f.get("_trashed") for f in rec["recording_files"]):
            return Response(status_code=204)
        rec["trashed"] = False
        for f in rec["recording_files"]:
            f.pop("_trashed", None)
        files.refresh_totals(rec)
        await hooks.emit(
            ctx, "recording.recovered", simulate.recording_object(rec), operator=principal.person
        )
        return Response(status_code=204)

    @app.put("/v2/meetings/{meeting:path}/recordings/{recording_id}/status")
    async def recover_file(request: Request, meeting: str, recording_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(RECOVER_FILE)
        rec = lookup.find_recording(ctx, meeting, include_trashed=True)
        lookup.ensure_recording_access(ctx, principal, admin, rec)
        if (await json_body(request)).get("action") != "recover":
            raise invalid_field("action")
        entry = next((f for f in rec["recording_files"] if f["id"] == recording_id), None)
        if entry is None:
            raise recording_missing()
        entry.pop("_trashed", None)
        if rec["trashed"] and not any(f.get("_trashed") for f in rec["recording_files"]):
            rec["trashed"] = False
        files.refresh_totals(rec)
        await hooks.emit(
            ctx, "recording.recovered", simulate.recording_object(rec), operator=principal.person
        )
        return Response(status_code=204)

    @app.delete("/v2/meetings/{meeting:path}/recordings/{recording_id}")
    async def delete_file(request: Request, meeting: str, recording_id: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(DELETE_FILE)
        rec = lookup.find_recording(ctx, meeting, include_trashed=True)
        lookup.ensure_recording_access(ctx, principal, admin, rec)
        action = request.query_params.get("action", "trash")
        if action not in ("trash", "delete"):
            raise invalid_field("action")
        candidates = rec["recording_files"] if action == "delete" else files.live_files(rec)
        entry = next((f for f in candidates if f["id"] == recording_id), None)
        if entry is None:
            raise recording_missing()
        snapshot = simulate.recording_object({**rec, "recording_files": [entry]})
        if action == "trash":
            entry["_trashed"] = isoformat_z(ctx.clock.now())
        else:
            rec["recording_files"].remove(entry)  # blobs are content addressed and shared: they stay
        files.refresh_totals(rec)
        await hooks.emit(
            ctx,
            "recording.trashed" if action == "trash" else "recording.deleted",
            snapshot,
            operator=principal.person,
        )
        return Response(status_code=204)

    @app.get("/v2/meetings/{meeting:path}/recordings")
    async def get_recordings(request: Request, meeting: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(READ_RECORDING)
        rec = lookup.find_recording(ctx, meeting)
        lookup.ensure_recording_access(ctx, principal, admin, rec)
        body = files.recording_wire(rec)
        wanted = {f.strip() for f in (request.query_params.get("include_fields") or "").split(",")}
        if "download_access_token" in wanted:
            ttl = query_int(request, "ttl", 86400, minimum=0)
            if ttl > MAX_TTL_S:
                raise zoom_error(400, E_VALIDATION, "Invalid parameter: ttl. The maximum is 604800 seconds.")
            body["download_access_token"] = hooks.issue_download_token(ctx, rec["id"], ttl)
        return respond(request, ctx, body)

    @app.delete("/v2/meetings/{meeting:path}/recordings")
    async def delete_recordings(request: Request, meeting: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(DELETE_RECORDING)
        rec = lookup.find_recording(ctx, meeting, include_trashed=True)
        lookup.ensure_recording_access(ctx, principal, admin, rec)
        action = request.query_params.get("action", "trash")
        if action not in ("trash", "delete"):
            raise invalid_field("action")
        snapshot = simulate.recording_object(rec)
        if action == "trash":
            stamp = isoformat_z(ctx.clock.now())
            rec["trashed"] = stamp
            for f in rec["recording_files"]:
                f["_trashed"] = stamp
        else:
            recordings.delete(rec["id"])
        await hooks.emit(
            ctx,
            "recording.trashed" if action == "trash" else "recording.deleted",
            snapshot,
            operator=principal.person,
        )
        return Response(status_code=204)
