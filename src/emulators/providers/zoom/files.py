"""Cloud-recording files: media, transcript (VTT), chat, timeline, and the recording record.

A *recording* is the cloud-recording aggregate of one meeting instance (UUID) holding
``recording_files``. The bytes live in ``ctx.store.blobs``; each file entry keeps its blob key
and download token in ``_private`` fields.
"""

from __future__ import annotations

import json
import uuid as uuidlib
from datetime import datetime, timedelta
from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext
from emulators.providers.zoom.common import WEB_HOST, digest
from emulators.providers.zoom.shapes import share_token
from emulators.world import media

RECORDING_SETTINGS = {
    "share_recording": "internally",
    "recording_authentication": False,
    "authentication_option": "",
    "authentication_domains": "",
    "viewer_download": True,
    "password": "",
    "on_demand": False,
    "approval_type": 2,
    "send_email_to_host": True,
    "show_social_share_buttons": True,
    "auto_delete": False,
}

#: file_type -> (extension, content type, recording_type)
FILE_KINDS: dict[str, tuple[str, str, str]] = {
    "MP4": ("MP4", media.MIME_MP4, "shared_screen_with_speaker_view"),
    "M4A": ("M4A", media.MIME_M4A, "audio_only"),
    "TRANSCRIPT": ("VTT", media.MIME_VTT, "audio_transcript"),
    "CHAT": ("TXT", "text/plain", "chat_file"),
    "TIMELINE": ("JSON", "application/json", "timeline"),
    "CC": ("VTT", media.MIME_VTT, "closed_caption"),
    "SUMMARY": ("JSON", "application/json", "summary"),
}


def clock_text(seconds: float, *, millis: bool = False) -> str:
    total = max(0.0, seconds)
    h, rest = divmod(int(total), 3600)
    m, s = divmod(rest, 60)
    text = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{text}.{round((total - int(total)) * 1000):03d}" if millis else text


def make_cues(turns: list[tuple[float, str, str]]) -> list[media.Cue]:
    """``(start_s, speaker, text)`` turns -> short, non-overlapping cues (Zoom splits long turns)."""
    cues: list[media.Cue] = []
    cursor = 0.0
    for start_s, speaker, text in turns:
        for cue in media.build_cues(
            [(speaker, text)], start_offset_s=max(start_s, cursor), gap_s=0.0, max_cue_s=7.0
        ):
            cues.append(cue)
            cursor = cue.end_s
    return cues


def cues_to_records(cues: list[media.Cue], emails: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "speaker": c.speaker,
            "speaker_email": emails.get(c.speaker, ""),
            "start_s": c.start_s,
            "end_s": c.end_s,
            "text": c.text,
        }
        for c in cues
    ]


def records_to_cues(records: list[dict[str, Any]]) -> list[media.Cue]:
    return [media.Cue(r["start_s"], r["end_s"], r["speaker"], r["text"]) for r in records]


def vtt_bytes(instance: dict[str, Any]) -> bytes:
    return media.make_vtt(records_to_cues(instance["transcript"]), cue_ids="index", with_voice_tags=False)


def chat_bytes(instance: dict[str, Any]) -> bytes:
    """Zoom's ``meeting_saved_chat.txt``: one tab-separated line per message."""
    lines = [
        f"{clock_text(m['offset_s'])}\t From  {m['name']} to  Everyone:\t{m['text']}"
        for m in instance["chat"]
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def timeline_bytes(instance: dict[str, Any], zoom_ids: dict[str, str]) -> bytes:
    entries: list[dict[str, Any]] = []
    last = None
    for cue in instance["transcript"]:
        if cue["speaker"] == last:
            continue
        last = cue["speaker"]
        entries.append(
            {
                "ts": clock_text(cue["start_s"], millis=True),
                "users": [{"username": cue["speaker"], "zoom_userid": zoom_ids.get(cue["speaker"], "")}],
            }
        )
    return json.dumps({"timeline": entries}, separators=(",", ":")).encode("utf-8")


def file_entry(
    ctx: ProviderContext,
    instance: dict[str, Any],
    kind: str,
    data: bytes,
    *,
    recording_start: datetime,
    recording_end: datetime,
    stem: str,
) -> dict[str, Any]:
    ext, content_type, rec_type = FILE_KINDS[kind]
    file_id = str(uuidlib.UUID(bytes=digest(ctx.world, "file", instance["id"], kind)[:16], version=4))
    token = share_token(ctx.world, "dl", instance["id"], kind, length=68)
    names = {
        "MP4": f"{stem}_Recording_1280x720.mp4",
        "M4A": f"{stem}_Recording.m4a",
        "TRANSCRIPT": f"{stem}_Recording.transcript.vtt",
        "CC": f"{stem}_Recording.cc.vtt",
        "CHAT": "meeting_saved_chat.txt",
        "TIMELINE": f"{stem}_Recording.timeline.json",
        "SUMMARY": f"{stem}_Summary.json",
    }
    entry: dict[str, Any] = {
        "id": file_id,
        "meeting_id": instance["id"],
        "recording_start": isoformat_z(recording_start),
        "recording_end": isoformat_z(recording_end),
        "file_type": kind,
        "file_extension": ext,
        "file_size": len(data),
    }
    if kind in ("MP4", "M4A"):
        entry["play_url"] = f"https://{WEB_HOST}/rec/play/{token}"
    entry["download_url"] = f"https://{WEB_HOST}/rec/download/{token}"
    entry["status"] = "completed"
    entry["recording_type"] = rec_type
    entry["_blob"] = ctx.store.blobs.put(data)
    entry["_content_type"] = content_type
    entry["_filename"] = names[kind]
    entry["_token"] = token
    return entry


def build_recording(
    ctx: ProviderContext, instance: dict[str, Any], *, only: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Create (and store) the cloud recording of ``instance`` from its transcript and chat."""
    world = ctx.world
    start = datetime.fromisoformat(instance["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(instance["end_time"].replace("Z", "+00:00"))
    rec_start = start + timedelta(seconds=instance.get("recording_offset_s", 5))
    seconds = max(1.0, (end - rec_start).total_seconds())
    stem = "GMT" + rec_start.strftime("%Y%m%d-%H%M%S")
    seed = int.from_bytes(digest(world, "media", instance["id"])[:4], "big")
    names = {p["name"]: p.get("user_id", "") for p in instance["participants"]}
    files: list[dict[str, Any]] = []

    def add(kind: str, data: bytes) -> None:
        if only is None or kind in only:
            files.append(
                file_entry(ctx, instance, kind, data, recording_start=rec_start, recording_end=end, stem=stem)
            )

    add("MP4", media.make_mp4(seconds, seed=seed, title=instance["topic"], created=rec_start))
    add("M4A", media.make_m4a(seconds, seed=seed, title=instance["topic"], created=rec_start))
    if instance.get("transcribed") and instance["transcript"]:
        add("TRANSCRIPT", vtt_bytes(instance))
    if instance["chat"]:
        add("CHAT", chat_bytes(instance))
    if instance.get("transcribed") and instance["transcript"]:
        add("TIMELINE", timeline_bytes(instance, names))
    record = {
        "id": instance["id"],
        "meeting_id": instance["meeting_id"],
        "account_id": world.org.zoom_account_id,
        "host_id": instance["host_id"],
        "host_email": instance["host_email"],
        "topic": instance["topic"],
        "type": instance["type"],
        "start_time": instance["start_time"],
        "timezone": instance["timezone"],
        "duration": instance["duration"],
        "share_url": f"https://{WEB_HOST}/rec/share/{share_token(world, 'share', instance['id'], length=58)}",
        "password": _recording_password(world, instance["id"]),
        "recording_play_passcode": share_token(world, "passcode", instance["id"], length=40),
        "recording_files": files,
        "trashed": False,
        "settings": {**RECORDING_SETTINGS, "password": _recording_password(world, instance["id"])},
    }
    refresh_totals(record)
    return record


def _recording_password(world: Any, key: str) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = digest(world, "recpwd", key)
    return "".join(alphabet[b % len(alphabet)] for b in raw[:8])


def live_files(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in record["recording_files"] if not f.get("_trashed")]


def refresh_totals(record: dict[str, Any]) -> None:
    files = live_files(record)
    record["total_size"] = sum(f["file_size"] for f in files)
    record["recording_count"] = len(files)


def recording_wire(record: dict[str, Any], *, trash_view: bool = False) -> dict[str, Any]:
    """The meeting-recordings object returned by the list and get endpoints."""
    files = [f for f in record["recording_files"] if f.get("_trashed")] if trash_view else live_files(record)
    wire_files = []
    for f in files:
        entry = {k: v for k, v in f.items() if not k.startswith("_")}
        if f.get("_trashed"):
            entry["deleted_time"] = f["_trashed"]
        wire_files.append(entry)
    item = {
        "uuid": record["id"],
        "id": record["meeting_id"],
        "account_id": record["account_id"],
        "host_id": record["host_id"],
        "topic": record["topic"],
        "type": str(record["type"]),
        "start_time": record["start_time"],
        "timezone": record["timezone"],
        "host_email": record["host_email"],
        "duration": record["duration"],
        "total_size": sum(f["file_size"] for f in files),
        "recording_count": len(files),
        "share_url": record["share_url"],
        "recording_files": wire_files,
        "password": record["password"],
        "recording_play_passcode": record["recording_play_passcode"],
    }
    return item
