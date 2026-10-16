"""Recording imports, expiring CDN links and speaker-attributed transcript downloads."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from emulators.core.clock import parse_instant
from emulators.core.provider import ProviderContext, url_for_host
from emulators.world import media

from .common import (
    SITE,
    authenticate,
    body,
    date_range,
    fail,
    get_meeting,
    in_range,
    is_admin,
    meeting_access,
    opaque,
    page,
    parent_matches,
    person,
    public,
    stamp,
    strings,
    time_wire,
)
from .hooks import emit
from .meetings import check_scope


def snippets(ctx: ProviderContext, transcript: dict[str, Any]) -> list[dict[str, Any]]:
    meeting = ctx.store.collection("meetings").get(transcript["meetingId"])
    assert meeting is not None
    turns = meeting.get("_turns", [])
    if not turns:
        turns = [
            {"speaker": meeting["hostUserId"], "offset": c.start_s, "duration": c.duration_s, "text": c.text}
            for c in media.build_cues(
                [(meeting["hostDisplayName"], meeting.get("agenda") or meeting["title"])]
            )
        ]
    result = []
    for i, turn in enumerate(turns):
        p = person(ctx, turn["speaker"])
        assert p is not None
        result.append(
            {
                "id": opaque("TRANSCRIPT_SNIPPET", f"{transcript['id']}/{i}"),
                "text": turn["text"],
                "personName": p["displayName"],
                "personEmail": p["emails"][0],
                "offsetMillisecond": round(turn["offset"] * 1000),
                "durationMillisecond": round(turn["duration"] * 1000),
            }
        )
    return result


def transcript_bytes(ctx: ProviderContext, transcript: dict[str, Any], kind: str) -> bytes:
    cache_key = f"_blob_{kind}"
    if cache_key not in transcript:
        rows = snippets(ctx, transcript)
        if kind == "vtt":
            cues = [
                media.Cue(
                    s["offsetMillisecond"] / 1000,
                    (s["offsetMillisecond"] + s["durationMillisecond"]) / 1000,
                    s["personName"],
                    s["text"],
                )
                for s in rows
            ]
            content = media.make_vtt(cues, with_voice_tags=False, cue_ids="index")
        else:
            content = ("\n".join(f"{s['personName']}: {s['text']}" for s in rows) + "\n").encode()
        transcript[cache_key] = ctx.store.blobs.put(content)
    return ctx.store.blobs.get(transcript[cache_key])


def recording_bytes(ctx: ProviderContext, recording: dict[str, Any], kind: str) -> bytes:
    cache_key = f"_blob_{kind}"
    if cache_key not in recording:
        if kind == "vtt":
            transcript = ctx.store.collection("transcripts").first(meetingId=recording["meetingId"])
            if not transcript:
                fail(404)
            content = transcript_bytes(ctx, transcript, "vtt")
        else:
            content = (media.make_mp4 if kind == "mp4" else media.make_m4a)(
                recording["durationSeconds"],
                title=recording["topic"],
                created=parse_instant(recording["timeRecorded"]),
            )
        recording[cache_key] = ctx.store.blobs.put(content)
        if kind == "mp4":
            recording["sizeBytes"] = len(content)
    return ctx.store.blobs.get(recording[cache_key])


def recording_wire(
    request: Request, ctx: ProviderContext, row: dict[str, Any], *, detail: bool = False
) -> dict[str, Any]:
    result = public(row)
    base = f"/recording/{row['id']}"
    result.update(
        downloadUrl=url_for_host(request, ctx, SITE, base + "/download"),
        playbackUrl=url_for_host(request, ctx, SITE, base + "/playback"),
    )
    if detail and row["status"] == "available":
        expiry = ctx.clock.now() + timedelta(
            seconds=int(ctx.setting("DOWNLOAD_TTL_SECONDS", "10800") or "10800")
        )
        token = secrets.token_urlsafe(32)
        ctx.store.collection("_download_links").put(
            {"id": token, "recordingId": row["id"], "expiration": stamp(expiry)}
        )
        links = {"expiration": stamp(expiry)}
        for name, ext in (
            ("recordingDownloadLink", "mp4"),
            ("audioDownloadLink", "m4a"),
            ("transcriptDownloadLink", "vtt"),
        ):
            links[name] = url_for_host(
                request,
                ctx,
                "binaries.webex.com",
                f"/recordings/{row['id']}/recording.{ext}?" + urlencode({"token": token}),
            )
        result["temporaryDirectDownloadLinks"] = links
    return time_wire(request, result)


def transcript_wire(request: Request, ctx: ProviderContext, row: dict[str, Any]) -> dict[str, Any]:
    result = public(row)
    for kind in ("vtt", "txt"):
        result[f"{kind}DownloadLink"] = url_for_host(
            request, ctx, "webexapis.com", f"/v1/meetingTranscripts/{row['id']}/download?format={kind}"
        )
    return time_wire(request, result)


def binary_response(request: Request, content: bytes, kind: str) -> Response:
    mime = {"mp4": media.MIME_MP4, "m4a": media.MIME_M4A, "vtt": media.MIME_VTT, "txt": "text/plain"}[kind]
    headers = {"Content-Disposition": f'attachment; filename="recording.{kind}"', "Accept-Ranges": "bytes"}
    status = 200
    range_header = request.headers.get("range")
    size = len(content)
    if range_header:
        try:
            unit, span = range_header.split("=", 1)
            first, last = span.split("-", 1)
            if unit != "bytes" or "," in span or (not first and not last):
                raise ValueError
            start = int(first) if first else max(0, size - int(last))
            end = min(int(last), size - 1) if last and first else size - 1
            if start < 0 or start > end or start >= size or (not first and int(last) <= 0):
                raise ValueError
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        content = content[start : end + 1]
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(len(content))
    return Response(
        b"" if request.method == "HEAD" else content, status_code=status, media_type=mime, headers=headers
    )


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/v1/recordings")
    async def recordings(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        check_scope(grant, "recordings")
        q = request.query_params
        bounds = date_range(request, ctx, defaults=not q.get("meetingId"))
        status = q.get("status", "available")
        if status not in ("available", "deleted") or q.get("format", "MP4") not in ("MP4", "ARF"):
            fail(400, "Invalid recording status or format.")
        if q.get("serviceType", "MeetingCenter") not in (
            "MeetingCenter",
            "EventCenter",
            "TrainingCenter",
            "SupportCenter",
        ):
            fail(400, "Invalid serviceType.")
        if q.get("meetingId"):
            get_meeting(ctx, q["meetingId"], grant, "recordings")
        if q.get("hostEmail"):
            user = person(ctx, grant.user_id)
            if (
                not user or q["hostEmail"].lower() not in [e.lower() for e in user["emails"]]
            ) and not is_admin(ctx, grant, "meeting:admin_recordings_read"):
                fail(403, "Filtering another host requires meeting:admin_recordings_read.")
        rows = []
        for row in ctx.store.collection("recordings"):
            meeting = ctx.store.collection("meetings").get(row["meetingId"])
            if not meeting or not meeting_access(ctx, grant, meeting, "recordings"):
                continue
            if q.get("meetingId") and not parent_matches(row, q["meetingId"]):
                continue
            if row["status"] != status or not in_range(row["createTime"], bounds):
                continue
            if not q.get("meetingId") and q.get("siteUrl", SITE) != row["siteUrl"]:
                continue
            if q.get("hostEmail") and q["hostEmail"].lower() != meeting["hostEmail"].lower():
                continue
            if q.get("format") and row["format"] != q["format"]:
                continue
            if q.get("topic") and q["topic"].casefold() not in row["topic"].casefold():
                continue
            if q.get("serviceType") and row["serviceType"] != q["serviceType"]:
                continue
            if q.get("integrationTag") and q["integrationTag"] not in row.get("integrationTags", []):
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r["createTime"], r["id"]), reverse=True)
        return page(request, [recording_wire(request, ctx, r) for r in rows], default=10, maximum=100)

    def get_recording(recording_id: str, request: Request, *, write: bool = False) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        check_scope(grant, "recordings", write=write)
        row = ctx.store.collection("recordings").get(recording_id)
        if not row:
            fail(404)
        get_meeting(ctx, row["meetingId"], grant, "recordings", write=write)
        host_email = request.query_params.get("hostEmail")
        meeting = ctx.store.collection("meetings").get(row["meetingId"])
        assert meeting is not None
        if host_email and host_email.lower() != meeting["hostEmail"].lower():
            fail(404)
        if request.headers.get("siteUrl", row["siteUrl"]) != row["siteUrl"]:
            fail(404)
        return row

    @app.get("/v1/recordings/{recording_id}")
    async def recording(recording_id: str, request: Request) -> dict[str, Any]:
        return recording_wire(request, ctx, get_recording(recording_id, request), detail=True)

    @app.delete("/v1/recordings/{recording_id}")
    async def delete(recording_id: str, request: Request) -> Response:
        row = get_recording(recording_id, request, write=True)
        meeting = ctx.store.collection("meetings").get(row["meetingId"])
        assert meeting is not None
        await emit(ctx, "recordings", "deleted", row, actor=meeting["hostUserId"])
        ctx.store.collection("recordings").delete(recording_id)
        return Response(status_code=204)

    @app.post("/v1/recordings/softDelete")
    @app.post("/v1/recordings/restore")
    @app.post("/v1/recordings/purge")
    async def recycle(request: Request) -> Response:
        grant = authenticate(request, ctx)
        check_scope(grant, "recordings", write=True)
        payload = await body(request)
        strings(payload, "siteUrl")
        operation = request.url.path.rsplit("/", 1)[-1]
        all_key = {"restore": "restoreAll", "purge": "purgeAll"}.get(operation)
        all_rows = payload.get(all_key, False) if all_key else False
        if not isinstance(all_rows, bool):
            fail(400, f"{all_key} must be a boolean.")
        ids = payload.get("recordingIds", [])
        if (
            not isinstance(ids, list)
            or not all(isinstance(i, str) and i for i in ids)
            or len(ids) > 100
            or (all_rows and ids)
            or (not all_rows and not ids)
        ):
            fail(400, "Specify 1 to 100 recordingIds, or the applicable all flag.")
        if all_rows:
            ids = [
                r["id"]
                for r in ctx.store.collection("recordings")
                if r["status"] == "deleted"
                and (m := ctx.store.collection("meetings").get(r["meetingId"]))
                and meeting_access(ctx, grant, m, "recordings", write=True)
                and r["siteUrl"] == payload.get("siteUrl", SITE)
            ]
        rows = [get_recording(i, request, write=True) for i in dict.fromkeys(ids)]
        expected = "available" if operation == "softDelete" else "deleted"
        if any(r["status"] != expected or r["siteUrl"] != payload.get("siteUrl", SITE) for r in rows):
            fail(400, "Recordings must belong to the specified site and have the required status.")
        for row in rows:
            if operation == "purge":
                ctx.store.collection("recordings").delete(row["id"])
            else:
                row["status"] = "available" if operation == "restore" else "deleted"
            # A restored resource gets new capabilities; old public links remain revoked.
            for link in ctx.store.collection("_download_links").where(recordingId=row["id"]):
                ctx.store.collection("_download_links").delete(link["id"])
        return Response(status_code=204)

    @app.api_route("/recordings/{recording_id}/recording.{kind}", methods=["GET", "HEAD"])
    async def download(recording_id: str, kind: str, request: Request) -> Response:
        link = ctx.store.collection("_download_links").get(request.query_params.get("token"))
        if (
            not link
            or link["recordingId"] != recording_id
            or parse_instant(link["expiration"]) <= ctx.clock.now()
        ):
            fail(403, "The temporary download link is invalid or has expired.")
        row = ctx.store.collection("recordings").get(recording_id)
        if not row or row["status"] != "available" or kind not in ("mp4", "m4a", "vtt"):
            fail(404)
        return binary_response(request, recording_bytes(ctx, row, kind), kind)

    @app.get("/recording/{recording_id}/{view}")
    async def web_page(recording_id: str, view: str, request: Request) -> HTMLResponse:
        row = get_recording(recording_id, request)
        if view not in ("download", "playback") or row["status"] != "available":
            fail(404)
        from html import escape

        return HTMLResponse(
            f"<!doctype html><title>{escape(row['topic'])}</title><h1>{escape(row['topic'])}</h1>"
        )

    @app.get("/v1/meetingTranscripts")
    async def transcripts(request: Request) -> JSONResponse:
        grant = authenticate(request, ctx)
        check_scope(grant, "transcripts")
        q = request.query_params
        bounds = date_range(request, ctx, defaults=not q.get("meetingId"))
        if q.get("meetingId"):
            meeting = get_meeting(ctx, q["meetingId"], grant, "transcripts")
            if meeting["meetingType"] != "meeting" or meeting["state"] != "ended":
                fail(400, "meetingId must identify an ended meeting instance.")
        if q.get("hostEmail") and not q.get("meetingId"):
            fail(400, "hostEmail requires meetingId for transcript lists.")
        rows = []
        for row in ctx.store.collection("transcripts"):
            meeting = ctx.store.collection("meetings").get(row["meetingId"])
            if not meeting or not meeting_access(ctx, grant, meeting, "transcripts"):
                continue
            if q.get("meetingId") and q["meetingId"] != row["meetingId"]:
                continue
            if q.get("hostEmail") and q["hostEmail"].lower() != meeting["hostEmail"].lower():
                continue
            if q.get("siteUrl", SITE) != row["siteUrl"]:
                continue
            if in_range(row["startTime"], bounds):
                rows.append(row)
        rows.sort(key=lambda r: r["startTime"], reverse=True)
        return page(request, [transcript_wire(request, ctx, r) for r in rows], default=10, maximum=100)

    def get_transcript(transcript_id: str, request: Request) -> dict[str, Any]:
        grant = authenticate(request, ctx)
        check_scope(grant, "transcripts")
        row = ctx.store.collection("transcripts").get(transcript_id)
        if not row:
            fail(404)
        get_meeting(ctx, row["meetingId"], grant, "transcripts")
        if request.query_params.get("hostEmail"):
            meeting = ctx.store.collection("meetings").get(row["meetingId"])
            if not meeting or request.query_params["hostEmail"].lower() != meeting["hostEmail"].lower():
                fail(404)
        return row

    @app.get("/v1/meetingTranscripts/{transcript_id}/download")
    async def transcript_download(transcript_id: str, request: Request) -> Response:
        row = get_transcript(transcript_id, request)
        kind = request.query_params.get("format", "vtt")
        if kind not in ("vtt", "txt"):
            fail(400, "format must be vtt or txt.")
        return binary_response(request, transcript_bytes(ctx, row, kind), kind)

    @app.get("/v1/meetingTranscripts/{transcript_id}/snippets")
    async def transcript_snippets(transcript_id: str, request: Request) -> JSONResponse:
        return page(request, snippets(ctx, get_transcript(transcript_id, request)), maximum=100)
