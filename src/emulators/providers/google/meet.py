"""Meet REST v2 conference artifacts projected from shared meetings."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from emulators.core.clock import isoformat_z, parse_instant
from emulators.core.ids import b64url, b64url_decode, stable_hash
from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.google.common import (
    conference_record_name,
    google_error,
    json_object,
    meet_code,
    meet_recording_file_id,
    meet_space_name,
    meet_transcript_doc_id,
    meet_uri,
    principal,
    user_id,
)
from emulators.providers.google.meet_filters import compile_filter
from emulators.world.model import Meeting


def public(record: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({k: v for k, v in record.items() if not k.startswith("_") and k != "id"})


def create_space(ctx: ProviderContext, owner: str, key: str, config: dict[str, Any]) -> dict[str, Any]:
    validate_config(config)
    name = meet_space_name(ctx.world, key)
    record = {
        "id": name,
        "name": name,
        "meetingUri": meet_uri(ctx.world, key),
        "meetingCode": meet_code(ctx.world, key),
        "config": {"accessType": "TRUSTED", "entryPointAccess": "ALL", **config},
        "_owner": owner,
        "_members": [owner],
        "_meeting": key,
    }
    validate_config(record["config"])
    ctx.store["meet_spaces"].put(record)
    return record


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise google_error(400, "Invalid space config", legacy=False)
    if config.get("accessType", "TRUSTED") not in ("OPEN", "TRUSTED", "RESTRICTED"):
        raise google_error(400, "Invalid accessType", legacy=False)
    if config.get("entryPointAccess", "ALL") not in ("ALL", "CREATOR_APP_ONLY"):
        raise google_error(400, "Invalid entryPointAccess", legacy=False)


def _artifacts(
    ctx: ProviderContext, meeting: Meeting, conference: dict[str, Any], *, force: bool = False
) -> None:
    name = conference["name"]
    start, end = conference["startTime"], conference.get("endTime")
    state = "FILE_GENERATED" if end else "STARTED"
    for kind, enabled in (("recordings", meeting.recorded), ("transcripts", meeting.transcribed)):
        if not enabled and not force:
            continue
        artifact_name = f"{name}/{kind}/{stable_hash(ctx.world.seed, meeting.id, kind, length=16)}"
        record: dict[str, Any] = {
            "id": artifact_name,
            "name": artifact_name,
            "state": state,
            "startTime": start,
            "_conference": name,
        }
        if end:
            record["endTime"] = end
        if state == "FILE_GENERATED":
            if kind == "recordings":
                file_id = meet_recording_file_id(ctx.world, meeting.id)
                record["driveDestination"] = {
                    "file": file_id,
                    "exportUri": f"https://drive.google.com/file/d/{file_id}/view",
                }
            else:
                doc_id = meet_transcript_doc_id(ctx.world, meeting.id)
                record["docsDestination"] = {
                    "document": doc_id,
                    "exportUri": f"https://docs.google.com/document/d/{doc_id}/view",
                }
        ctx.store[f"meet_{kind}"].put(record)
        if kind == "transcripts":
            for index, turn in enumerate(meeting.transcript):
                entry_name = f"{artifact_name}/entries/{index + 1}"
                ctx.store["meet_entries"].put(
                    {
                        "id": entry_name,
                        "name": entry_name,
                        "participant": _participant_name(ctx, name, turn.speaker_id),
                        "text": turn.text,
                        "languageCode": "en-US",
                        "startTime": isoformat_z(parse_instant(start) + timedelta(seconds=turn.offset_s)),
                        "endTime": isoformat_z(
                            parse_instant(start) + timedelta(seconds=turn.offset_s + turn.duration_s)
                        ),
                        "_transcript": artifact_name,
                    }
                )


def _participant_name(ctx: ProviderContext, conference: str, person_id: str) -> str:
    return f"{conference}/participants/{stable_hash(ctx.world.seed, 'meet-participant', person_id)}"


def _conference(ctx: ProviderContext, meeting: Meeting, *, live: bool = False) -> dict[str, Any]:
    name = conference_record_name(ctx.world, meeting.id)
    record = {
        "id": name,
        "name": name,
        "startTime": isoformat_z(ctx.clock.now() if live else meeting.start),
        "expireTime": isoformat_z((ctx.clock.now() if live else meeting.start) + timedelta(days=30)),
        "space": meet_space_name(ctx.world, meeting.id),
        "_code": meet_code(ctx.world, meeting.id),
        "_members": list(meeting.all_attendee_ids()),
        "_meeting": meeting.id,
    }
    if not live:
        record["endTime"] = isoformat_z(meeting.end)
    ctx.store["meet_conferences"].put(record)
    participants = list(meeting.participations)
    for part in participants:
        person = ctx.world.person(part.person_id)
        participant_name = _participant_name(ctx, name, person.id)
        existing = ctx.store["meet_participants"].get(participant_name)
        p: dict[str, Any] = existing or {
            "id": participant_name,
            "name": participant_name,
            "earliestStartTime": isoformat_z(ctx.clock.now() if live else part.joined),
            "_conference": name,
            "_person": person.id,
        }
        if part.device == "phone":
            p["phoneUser"] = {"displayName": person.display_name}
        elif person.is_external:
            p["anonymousUser"] = {"displayName": person.display_name}
        else:
            p["signedinUser"] = {
                "user": "users/" + user_id(ctx.world, person.id),
                "displayName": person.display_name,
            }
        if not live:
            p["latestEndTime"] = max(p.get("latestEndTime", ""), isoformat_z(part.left))
        else:
            p.pop("latestEndTime", None)
        ctx.store["meet_participants"].put(p)
        number = len(ctx.store["meet_sessions"].where(_participant=participant_name)) + 1
        session_name = f"{participant_name}/participantSessions/{number}"
        session = {
            "id": session_name,
            "name": session_name,
            "startTime": isoformat_z(ctx.clock.now() if live else part.joined),
            "_participant": participant_name,
        }
        if not live:
            session["endTime"] = isoformat_z(part.left)
        ctx.store["meet_sessions"].put(session)
    _artifacts(ctx, meeting, record)
    return record


def seed(ctx: ProviderContext) -> None:
    for meeting in ctx.world.meetings:
        if meeting.platform != "meet":
            continue
        space = create_space(ctx, meeting.organizer_id, meeting.id, {})
        space["_members"] = list(meeting.all_attendee_ids())
        if meeting.happened and not meeting.cancelled:
            _conference(ctx, meeting)


def _identity(request: Request, ctx: ProviderContext) -> str:
    actor = principal(request, ctx, legacy=False)
    if (
        not actor.person_id
        or actor.app_only
        or not any(p.id == actor.person_id and p.active for p in ctx.world.people)
    ):
        raise google_error(403, "Google Meet requires a user credential.", legacy=False)
    if actor.scopes:
        allowed = {"https://www.googleapis.com/auth/meetings.space.created"}
        if request.method == "GET":
            allowed.add("https://www.googleapis.com/auth/meetings.space.readonly")
        if not allowed.intersection(actor.scopes):
            raise google_error(403, "Request had insufficient authentication scopes.", legacy=False)
    return actor.person_id


def _get(ctx: ProviderContext, collection: str, name: str) -> dict[str, Any]:
    record = ctx.store[collection].get(name)
    if record is None:
        raise google_error(404, "Requested entity was not found.", legacy=False)
    return record


def _authorize(request: Request, ctx: ProviderContext, name: str) -> dict[str, Any]:
    actor = _identity(request, ctx)
    conf = _get(ctx, "meet_conferences", name)
    if actor not in conf["_members"]:
        raise google_error(403, "The caller does not have permission", legacy=False)
    return conf


def _page(
    request: Request, rows: list[dict[str, Any]], key: str, default: int, maximum: int
) -> dict[str, Any]:
    try:
        size = int(request.query_params.get("pageSize", default))
        if size < 0:
            raise ValueError
        size = min(size or default, maximum)
        token = request.query_params.get("pageToken")
        binding = stable_hash(request.url.path, request.query_params.get("filter", ""))
        decoded = b64url_decode(token).decode().split(":") if token else [binding, "0"]
        if decoded[0] != binding:
            raise ValueError
        offset = int(decoded[1])
        if offset < 0:
            raise ValueError
    except (ValueError, UnicodeError, IndexError) as exc:
        raise google_error(400, "Invalid page token or page size", legacy=False) from exc
    result: dict[str, Any] = {}
    if rows[offset : offset + size]:
        result[key] = [public(r) for r in rows[offset : offset + size]]
    if offset + size < len(rows):
        result["nextPageToken"] = b64url(f"{binding}:{offset + size}".encode())
    return result


def _rewrite_links(request: Request, ctx: ProviderContext, record: dict[str, Any]) -> dict[str, Any]:
    result = public(record)
    if "meetingCode" in result:
        result["meetingUri"] = url_for_host(request, ctx, "meet.google.com", "/" + result["meetingCode"])
    for destination, host, path in (
        ("driveDestination", "drive.google.com", "/file/d/{id}/view"),
        ("docsDestination", "docs.google.com", "/document/d/{id}/view"),
    ):
        if destination in result:
            data = result[destination]
            value = data.get("file") or data.get("document")
            data["exportUri"] = url_for_host(request, ctx, host, path.format(id=value))
    return result


def register(app: FastAPI, ctx: ProviderContext) -> None:
    old_http = app.exception_handlers[HTTPException]
    old_validation = app.exception_handlers[RequestValidationError]

    @app.exception_handler(HTTPException)
    async def meet_http_error(request: Request, exc: HTTPException) -> Any:
        if request.url.path.startswith("/v2/"):
            from emulators.providers.google.common import error_body

            return JSONResponse(
                error_body(exc.status_code, str(exc.detail), legacy=False), status_code=exc.status_code
            )
        return await old_http(request, exc)

    @app.exception_handler(RequestValidationError)
    async def meet_validation_error(request: Request, exc: RequestValidationError) -> Any:
        if request.url.path.startswith("/v2/"):
            from emulators.providers.google.common import error_body

            return JSONResponse(error_body(400, "Invalid request", legacy=False), status_code=400)
        return await old_validation(request, exc)

    @app.post("/v2/spaces")
    async def spaces_create(request: Request) -> dict[str, Any]:
        actor = _identity(request, ctx)
        payload = await json_object(request, legacy=False)
        key = "created-" + ctx.ids.hex(16)
        return _rewrite_links(request, ctx, create_space(ctx, actor, key, payload.get("config", {})))

    @app.api_route("/v2/spaces/{space_id}", methods=["GET", "PATCH"])
    async def spaces_get(request: Request, space_id: str) -> dict[str, Any]:
        actor = _identity(request, ctx)
        space = ctx.store["meet_spaces"].get("spaces/" + space_id)
        space = space or ctx.store["meet_spaces"].first(meetingCode=space_id)
        if not space:
            raise google_error(404, "Requested entity was not found.", legacy=False)
        if actor not in space["_members"]:
            raise google_error(403, "The caller does not have permission", legacy=False)
        if request.method == "PATCH":
            if actor != space["_owner"]:
                raise google_error(403, "Only the space owner can update the space.", legacy=False)
            payload = await json_object(request, legacy=False)
            if "config" in payload:
                validate_config(payload["config"])
            mask = request.query_params.get("updateMask", "")
            if not mask:
                raise google_error(400, "updateMask is required", legacy=False)
            config = dict(space["config"])
            for field in mask.split(","):
                if field in ("*", "config"):
                    config = {"accessType": "TRUSTED", "entryPointAccess": "ALL", **payload.get("config", {})}
                elif field in ("config.accessType", "config.access_type"):
                    config["accessType"] = payload.get("config", {}).get("accessType", "TRUSTED")
                elif field in ("config.entryPointAccess", "config.entry_point_access"):
                    config["entryPointAccess"] = payload.get("config", {}).get("entryPointAccess", "ALL")
                else:
                    raise google_error(400, "Invalid updateMask", legacy=False)
            validate_config(config)
            space["config"] = config
        return _rewrite_links(request, ctx, space)

    @app.post("/v2/spaces/{space_id}:endActiveConference")
    async def spaces_end(request: Request, space_id: str) -> dict[str, Any]:
        actor = _identity(request, ctx)
        space = _get(ctx, "meet_spaces", "spaces/" + space_id)
        if actor != space["_owner"]:
            raise google_error(403, "The caller does not have permission", legacy=False)
        if "activeConference" not in space:
            raise google_error(400, "No active conference", legacy=False)
        await end_conference(ctx, {"meeting_id": space["_meeting"]})
        return {}

    @app.get("/v2/conferenceRecords")
    async def conferences_list(request: Request) -> dict[str, Any]:
        actor = _identity(request, ctx)
        predicate = compile_filter(
            request.query_params.get("filter", ""),
            {
                "space.name": "space",
                "space.meeting_code": "_code",
                "start_time": "startTime",
                "end_time": "endTime",
            },
        )
        rows = [r for r in ctx.store["meet_conferences"] if actor in r["_members"] and predicate(r)]
        rows.sort(key=lambda r: r["startTime"], reverse=True)
        return _page(request, rows, "conferenceRecords", 25, 100)

    @app.get("/v2/conferenceRecords/{conference_id}")
    async def conferences_get(request: Request, conference_id: str) -> dict[str, Any]:
        return public(_authorize(request, ctx, "conferenceRecords/" + conference_id))

    @app.get("/v2/conferenceRecords/{conference_id}/{area}")
    async def artifacts_list(request: Request, conference_id: str, area: str) -> dict[str, Any]:
        name = "conferenceRecords/" + conference_id
        _authorize(request, ctx, name)
        if area not in ("participants", "recordings", "transcripts"):
            raise google_error(404, "Requested entity was not found.", legacy=False)
        fields = {"earliest_start_time": "earliestStartTime", "latest_end_time": "latestEndTime"}
        predicate = compile_filter(
            request.query_params.get("filter", ""), fields if area == "participants" else {}
        )
        rows = [r for r in ctx.store[f"meet_{area}"].where(_conference=name) if predicate(r)]
        rows.sort(
            key=lambda r: r.get("earliestStartTime", r.get("startTime", "")), reverse=area == "participants"
        )
        result = _page(
            request, rows, area, 100 if area == "participants" else 10, 250 if area == "participants" else 100
        )
        if area in result:
            result[area] = [_rewrite_links(request, ctx, r) for r in result[area]]
        return result

    @app.get("/v2/conferenceRecords/{conference_id}/{area}/{artifact_id}")
    async def artifacts_get(
        request: Request, conference_id: str, area: str, artifact_id: str
    ) -> dict[str, Any]:
        name = "conferenceRecords/" + conference_id
        _authorize(request, ctx, name)
        if area not in ("participants", "recordings", "transcripts"):
            raise google_error(404, "Requested entity was not found.", legacy=False)
        return _rewrite_links(request, ctx, _get(ctx, f"meet_{area}", f"{name}/{area}/{artifact_id}"))

    @app.get("/v2/conferenceRecords/{conference_id}/{area}/{artifact_id}/{child}")
    async def children_list(
        request: Request, conference_id: str, area: str, artifact_id: str, child: str
    ) -> dict[str, Any]:
        parent, collection, field, default, maximum = child_info(conference_id, area, artifact_id, child)
        _authorize(request, ctx, "conferenceRecords/" + conference_id)
        _get(ctx, "meet_" + area, parent)
        rows = ctx.store[collection].where(**{field: parent})
        if child == "participantSessions":
            predicate = compile_filter(
                request.query_params.get("filter", ""), {"start_time": "startTime", "end_time": "endTime"}
            )
            rows = [r for r in rows if predicate(r)]
            rows.sort(key=lambda r: r["startTime"], reverse=True)
        else:
            rows.sort(key=lambda r: r["startTime"])
        return _page(request, rows, "transcriptEntries" if child == "entries" else child, default, maximum)

    @app.get("/v2/conferenceRecords/{conference_id}/{area}/{artifact_id}/{child}/{child_id}")
    async def children_get(
        request: Request, conference_id: str, area: str, artifact_id: str, child: str, child_id: str
    ) -> dict[str, Any]:
        parent, collection, _, _, _ = child_info(conference_id, area, artifact_id, child)
        _authorize(request, ctx, "conferenceRecords/" + conference_id)
        return public(_get(ctx, collection, f"{parent}/{child}/{child_id}"))

    def child_info(
        conference_id: str, area: str, artifact_id: str, child: str
    ) -> tuple[str, str, str, int, int]:
        parent = f"conferenceRecords/{conference_id}/{area}/{artifact_id}"
        if (area, child) == ("participants", "participantSessions"):
            return parent, "meet_sessions", "_participant", 100, 250
        if (area, child) == ("transcripts", "entries"):
            return parent, "meet_entries", "_transcript", 10, 100
        raise google_error(404, "Requested entity was not found.", legacy=False)

    ctx.provider.actions["start_conference"] = start_conference
    ctx.provider.actions["end_conference"] = end_conference


async def start_conference(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Start a seeded Meet meeting, exposing active participants and artifacts."""
    meeting = next(
        (m for m in ctx.world.meetings if m.id == payload.get("meeting_id") and m.platform == "meet"), None
    )
    if not meeting:
        raise google_error(404, "Meeting not found", legacy=False)
    name = conference_record_name(ctx.world, meeting.id)
    for area in ("participants", "sessions", "recordings", "transcripts", "entries"):
        for row in ctx.store[f"meet_{area}"].values():
            if row["name"].startswith(name + "/"):
                ctx.store[f"meet_{area}"].delete(row["id"])
    conf = _conference(ctx, meeting, live=True)
    _artifacts(ctx, meeting, conf, force=True)
    # Future meetings have no historical participation records yet.
    if not meeting.participations:
        for pid in meeting.all_attendee_ids():
            person = ctx.world.person(pid)
            pname = _participant_name(ctx, name, pid)
            ctx.store["meet_participants"].put(
                {
                    "id": pname,
                    "name": pname,
                    "earliestStartTime": conf["startTime"],
                    "signedinUser": {
                        "user": "users/" + user_id(ctx.world, pid),
                        "displayName": person.display_name,
                    },
                    "_conference": name,
                    "_person": pid,
                }
            )
            session = pname + "/participantSessions/1"
            ctx.store["meet_sessions"].put(
                {"id": session, "name": session, "startTime": conf["startTime"], "_participant": pname}
            )
    space = _get(ctx, "meet_spaces", conf["space"])
    space["activeConference"] = {"conferenceRecord": name}
    return public(conf)


async def end_conference(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """End an active conference and finalize its configured recording and transcript."""
    meeting = next(
        (m for m in ctx.world.meetings if m.id == payload.get("meeting_id") and m.platform == "meet"), None
    )
    if not meeting:
        raise google_error(404, "Meeting not found", legacy=False)
    conf = _get(ctx, "meet_conferences", conference_record_name(ctx.world, meeting.id))
    space = _get(ctx, "meet_spaces", conf["space"])
    if "activeConference" not in space:
        raise google_error(400, "No active conference", legacy=False)
    conf["endTime"] = isoformat_z(ctx.clock.now())
    space.pop("activeConference", None)
    for part in ctx.store["meet_participants"].where(_conference=conf["name"]):
        part["latestEndTime"] = conf["endTime"]
        for session in ctx.store["meet_sessions"].where(_participant=part["name"]):
            session.setdefault("endTime", conf["endTime"])
    _artifacts(ctx, meeting, conf, force=True)
    return public(conf)
