"""AI Companion meeting summaries (``/v2/meetings/{uuid}/meeting_summary``), built from the transcript."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.provider import ProviderContext
from emulators.providers.zoom import lookup
from emulators.providers.zoom.common import (
    LiveCollection,
    Need,
    authenticate,
    decode_path_key,
    no_permission,
    paginate,
    respond,
    zoom_error,
)
from emulators.providers.zoom.timeutil import day_range, in_days, parse_instant

READ_SUMMARY = Need("meeting_summary:read", "meeting:read:summary")
LIST_SUMMARIES = Need("meeting_summary:read", "meeting:read:list_summaries", admin_only=True)
E_SUMMARY_MISSING = 3322

LABELS = (
    ("request list", "Open requests"),
    ("revenue", "Revenue testing"),
    ("timeline", "Timeline and deliverables"),
    ("pipeline", "Pipeline"),
    ("utilisation", "Staffing and utilisation"),
    ("collections", "Collections"),
    ("lease", "Lease accounting"),
    ("questions", "Questions"),
    ("announcement", "Announcements"),
    ("action item", "Action items"),
    ("recap", "Recap"),
    ("development", "Development"),
)
ACTION_WORDS = re.compile(
    r"\b(I'll|I will|we'll|we will|will send|follow up|by (Monday|Tuesday|Wednesday|Thursday|Friday)|action items?)\b",
    re.I,
)
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def turns_of(instance: dict[str, Any]) -> list[tuple[str, str]]:
    """Merge consecutive cues of one speaker back into turns."""
    turns: list[tuple[str, str]] = []
    for cue in instance["transcript"]:
        if turns and turns[-1][0] == cue["speaker"]:
            turns[-1] = (cue["speaker"], f"{turns[-1][1]} {cue['text']}")
        else:
            turns.append((cue["speaker"], cue["text"]))
    return turns


def first_sentence(text: str, limit: int = 220) -> str:
    sentence = SENTENCE.split(text.strip())[0]
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def label_for(chunk: list[tuple[str, str]], index: int) -> str:
    blob = " ".join(t for _, t in chunk).lower()
    for needle, label in LABELS:
        if needle in blob:
            return label
    return f"Discussion {index + 1}"


def build_summary(instance: dict[str, Any]) -> dict[str, Any] | None:
    turns = turns_of(instance)
    if not turns:
        return None
    speakers = list(dict.fromkeys(name for name, _ in turns))
    host = speakers[0]
    others = ", ".join(speakers[1:3])
    overview = (
        f"{host} opened the meeting on {instance['topic']}"
        + (f" with {others}" if others else "")
        + f". {first_sentence(turns[0][1])}"
    )
    size = max(1, -(-len(turns) // 4))
    details = []
    for i in range(0, len(turns), size):
        chunk = turns[i : i + size]
        label = label_for(chunk, i // size)
        sentences = [f"{who} said: {first_sentence(text)}" for who, text in chunk[:2]]
        details.append({"label": label, "summary": " ".join(sentences)})
    steps = []
    for who, text in turns:
        for sentence in SENTENCE.split(text):
            if ACTION_WORDS.search(sentence) and len(steps) < 5:
                steps.append(f"{who}: {sentence.strip()}")
    return {
        "summary_title": f"Meeting summary for {instance['topic']}",
        "summary_overview": overview,
        "summary_details": details,
        "next_steps": steps or ["Follow up on the items discussed."],
    }


def summary_header(instance: dict[str, Any]) -> dict[str, Any]:
    start = parse_instant(instance["start_time"])
    end = parse_instant(instance["end_time"])
    created = end + timedelta(minutes=3)

    def fmt(moment: Any) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "meeting_host_id": instance["host_id"],
        "meeting_host_email": instance["host_email"],
        "meeting_uuid": instance["id"],
        "meeting_id": instance["meeting_id"],
        "meeting_topic": instance["topic"],
        "meeting_start_time": fmt(start),
        "meeting_end_time": fmt(end),
        "summary_start_time": fmt(start + timedelta(seconds=instance.get("recording_offset_s", 5))),
        "summary_end_time": fmt(end),
        "summary_created_time": fmt(created),
        "summary_last_modified_time": fmt(created),
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    instances = LiveCollection(ctx, "instances")

    def summarised() -> list[dict[str, Any]]:
        return [
            i for i in instances if i.get("state") == "ended" and i.get("transcribed") and i["transcript"]
        ]

    @app.get("/v2/meetings/meeting_summaries")
    async def list_summaries(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(LIST_SUMMARIES):
            raise no_permission()
        q = request.query_params
        today = ctx.clock.now().date()
        lo, hi = day_range(
            q.get("from"), q.get("to"), default_from=today - timedelta(days=30), default_to=today
        )
        rows = [i for i in summarised() if in_days(i["start_time"], lo, hi)]
        if q.get("host_id"):
            rows = [i for i in rows if i["host_id"] == q["host_id"]]
        rows.sort(key=lambda i: i["start_time"], reverse=True)
        page, meta = paginate(request, rows)
        body = {
            "next_page_token": meta["next_page_token"],
            "page_size": meta["page_size"],
            "total_records": meta["total_records"],
            "from": lo.isoformat(),
            "to": hi.isoformat(),
            "summaries": [summary_header(i) for i in page],
        }
        return respond(request, ctx, body)

    @app.get("/v2/meetings/{rest:path}/meeting_summary")
    async def meeting_summary(request: Request, rest: str) -> Response:
        principal = authenticate(ctx, request)
        admin = principal.authorize(READ_SUMMARY)
        inst = lookup.find_instance(ctx, rest, ended_only=True)
        if inst.get("state") != "ended":
            raise zoom_error(
                404, E_SUMMARY_MISSING, f"The meeting summary does not exist: {decode_path_key(rest)}."
            )
        mine = principal.person is not None and inst["host_id"] == lookup.my_user_id(ctx, principal)
        if not (admin or mine):
            raise no_permission()
        built = build_summary(inst) if inst.get("transcribed") else None
        if built is None:
            raise zoom_error(404, E_SUMMARY_MISSING, "The meeting summary does not exist.")
        return respond(request, ctx, {**summary_header(inst), **built})
