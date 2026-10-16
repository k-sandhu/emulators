"""Synthetic recordings and transcripts that real media parsers accept.

Meeting-platform emulators (Zoom cloud recordings, Teams ``onlineMeeting`` recordings and
transcripts, Google Meet artefacts) hand these bytes to consumers that pipe them into
ffmpeg, mutagen-style tag readers and transcription/diarization stacks.  Opaque placeholder
bytes would blow up in those tools, so everything here is a structurally valid file, built
with the standard library only (no ffmpeg at runtime) and fully deterministic: the same
arguments always give the same bytes, which keeps golden files and content hashes stable.

* :func:`make_wav` - mono 16-bit PCM WAV with an optional quiet, speech-like tone.
* :func:`make_m4a` / :func:`make_mp4` - ISO-BMFF audio files whose payload is genuine AAC-LC
  (silent raw frames, 4 bytes per 1024 samples) so ffmpeg decodes them without complaint.
* :func:`make_vtt` / :func:`make_srt` / :func:`parse_vtt` - transcript formats, in the Teams
  (``<v Speaker>`` voice tags, GUID-like cue ids) and Zoom (``Speaker: text``, numeric ids) styles.
* :func:`build_cues` / :func:`estimate_speech_duration` - turn dialogue into timed cues.
"""

from __future__ import annotations

import io
import math
import random
import re
import struct
import uuid
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

__all__ = [
    "MIME_M4A",
    "MIME_MP4",
    "MIME_SRT",
    "MIME_VTT",
    "MIME_WAV",
    "Cue",
    "CueIdStyle",
    "build_cues",
    "estimate_speech_duration",
    "make_m4a",
    "make_mp4",
    "make_srt",
    "make_vtt",
    "make_wav",
    "parse_vtt",
]

MIME_WAV = "audio/wav"
MIME_M4A = "audio/mp4"
MIME_MP4 = "video/mp4"
MIME_VTT = "text/vtt"
MIME_SRT = "application/x-subrip"

CueIdStyle = Literal["uuid", "index", "none"]

# ---------------------------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------------------------

_PCM_PEAK = 0.2  # of full scale: quiet (about -14 dBFS peak, roughly -26 dBFS RMS) like real speech
_SYLLABLE_BANK_SIZE = 12
_HARMONIC_WEIGHTS = (1.0, 0.55, 0.4, 0.25, 0.15)


def _syllable(rng: random.Random, sample_rate: int) -> bytes:
    """One voiced "syllable" of 16-bit little-endian PCM.

    The fundamental is chosen so the syllable holds a whole number of periods, which makes every
    harmonic start and end on a zero crossing: syllables can be concatenated in any order without
    clicks, and a 160-300 ms length gives a natural 3-6 Hz syllable-rate amplitude envelope.
    """
    length = max(2, round(sample_rate * (0.16 + 0.14 * rng.random())))
    cycles = max(1, round((95.0 + 125.0 * rng.random()) * length / sample_rate))
    f0 = cycles * sample_rate / length
    weights = [w * (0.8 + 0.4 * rng.random()) for w in _HARMONIC_WEIGHTS]
    # Keep partials safely below Nyquist (matters for very low sample rates only).
    partials = [(k + 1, w) for k, w in enumerate(weights) if (k + 1) * f0 < 0.45 * sample_rate] or [(1, 1.0)]
    norm = sum(w for _, w in partials)
    stress = 0.6 + 0.4 * rng.random()
    scale = 32767 * _PCM_PEAK * stress / norm
    samples: list[int] = []
    for i in range(length):
        envelope = 0.3 + 0.7 * math.sin(math.pi * i / length) ** 2
        phase = 2.0 * math.pi * cycles * i / length
        value = sum(w * math.sin(k * phase) for k, w in partials)
        samples.append(max(-32768, min(32767, round(scale * envelope * value))))
    return struct.pack(f"<{length}h", *samples)


def _speech_like_pcm(n_samples: int, sample_rate: int, seed: int) -> bytes:
    """``n_samples`` of deterministic, quiet, speech-like mono PCM (voiced syllables and pauses)."""
    rng = random.Random(seed)  # only .random() is used: its sequence is stable across Python versions
    bank = [_syllable(rng, sample_rate) for _ in range(_SYLLABLE_BANK_SIZE)]
    want = n_samples * 2
    chunks: list[bytes] = []
    produced = 0
    while produced < want:
        chunk = bank[int(rng.random() * len(bank))]
        chunks.append(chunk)
        produced += len(chunk)
        if rng.random() < 0.12:  # occasional short pause between words
            pause = bytes(2 * round(sample_rate * (0.08 + 0.17 * rng.random())))
            chunks.append(pause)
            produced += len(pause)
    return b"".join(chunks)[:want]


def make_wav(duration_s: float, *, sample_rate: int = 16000, seed: int = 0, tone: bool = True) -> bytes:
    """Mono 16-bit PCM WAV of ``duration_s`` seconds.

    With ``tone`` the samples are a quiet, speech-like signal (harmonic partials under a
    syllable-rate envelope, with short pauses) so energy/VAD based tools detect activity;
    otherwise digital silence.  ``seed`` selects the pitch pattern; output is deterministic.
    """
    if duration_s < 0:
        raise ValueError("duration_s must be >= 0")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    n_samples = round(duration_s * sample_rate)
    pcm = _speech_like_pcm(n_samples, sample_rate, seed) if tone and n_samples else bytes(2 * n_samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------
# ISO-BMFF (M4A / MP4) with AAC-LC audio
# ---------------------------------------------------------------------------------------------

_AAC_FRAME_SAMPLES = 1024  # samples per AAC-LC access unit (frameLengthFlag = 0)
_AAC_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
_MAC_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)  # ISO-BMFF timestamps count seconds from here
_DEFAULT_CREATED = datetime(2024, 1, 1, tzinfo=UTC)
_UNITY_MATRIX = struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
_MOVIE_TIMESCALE = 1000
_DEFAULT_AUDIO_SAMPLE_RATE = 16000  # 16 kHz keeps an hour of audio near 225 KB (4 bytes per frame)


def _silent_aac_frame(global_gain: int) -> bytes:
    """A 4-byte AAC-LC raw_data_block that decodes to 1024 samples of digital silence.

    Bit layout (ISO/IEC 14496-3, mono ``single_channel_element``), MSB first, 32 bits total::

        id_syn_ele              3  000        ID_SCE
        element_instance_tag    4  0000
        global_gain             8  gggggggg   any value; irrelevant because no spectrum follows
        ics_reserved_bit        1  0
        window_sequence         2  00         ONLY_LONG_SEQUENCE
        window_shape            1  1          KBD window (matches the widely used FAAC silence frame)
        max_sfb                 6  000000     zero scalefactor bands => no section/scalefactor/spectral data
        predictor_data_present  1  0
        pulse_data_present      1  0
        tns_data_present        1  0
        gain_control_present    1  0
        id_syn_ele              3  111        ID_END

    With ``global_gain`` 160 this is the well-known ``01 40 20 07`` frame.
    """
    return struct.pack(">I", (global_gain << 17) | (1 << 13) | 0b111)


def _box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _full_box(kind: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags), *parts)


def _descriptor(tag: int, body: bytes) -> bytes:
    """MPEG-4 systems descriptor using the 4-byte expandable length form ffmpeg and Apple write."""
    n = len(body)
    length = bytes([0x80 | ((n >> shift) & 0x7F) for shift in (21, 14, 7)] + [n & 0x7F])
    return bytes([tag]) + length + body


def _esds(sample_rate_index: int, frame_bytes: int, bitrate: int) -> bytes:
    """``esds`` box carrying the AudioSpecificConfig: AAC-LC, given rate index, 1 channel."""
    # audioObjectType(5)=2 AAC LC | samplingFrequencyIndex(4) | channelConfiguration(4)=1 mono
    # | frameLengthFlag(1)=0 (1024) | dependsOnCoreCoder(1)=0 | extensionFlag(1)=0
    asc = struct.pack(">H", (2 << 11) | (sample_rate_index << 7) | (1 << 3))
    decoder_config = struct.pack(">BB3sII", 0x40, 0x15, frame_bytes.to_bytes(3, "big"), bitrate, bitrate)
    decoder_config += _descriptor(0x05, asc)
    es = struct.pack(">HB", 1, 0) + _descriptor(0x04, decoder_config) + _descriptor(0x06, b"\x02")
    return _full_box(b"esds", 0, 0, _descriptor(0x03, es))


def _ilst_title(title: str) -> bytes:
    """``udta/meta/ilst/(c)nam`` iTunes-style metadata, which mutagen and ffprobe read as the title."""
    data = _box(b"data", struct.pack(">II", 1, 0), title.encode("utf-8"))  # type 1 = UTF-8, locale 0
    handler = _full_box(b"hdlr", 0, 0, struct.pack(">I4s4sII", 0, b"mdir", b"appl", 0, 0), b"\x00")
    return _box(b"udta", _full_box(b"meta", 0, 0, handler, _box(b"ilst", _box(b"\xa9nam", data))))


def _moov(
    *,
    created: int,
    movie_ms: int,
    n_frames: int,
    sample_rate: int,
    rate_index: int,
    frame_bytes: int,
    mdat_offset: int,
    title: str,
) -> bytes:
    media_duration = n_frames * _AAC_FRAME_SAMPLES
    bitrate = round(frame_bytes * 8 * sample_rate / _AAC_FRAME_SAMPLES)

    mvhd = _full_box(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", created, created, _MOVIE_TIMESCALE, movie_ms),
        struct.pack(">IH", 0x00010000, 0x0100),  # rate 1.0, volume 1.0
        bytes(10),  # reserved
        _UNITY_MATRIX,
        bytes(24),  # pre_defined
        struct.pack(">I", 2),  # next_track_ID
    )
    tkhd = _full_box(
        b"tkhd",
        0,
        0x000003,  # enabled | in movie
        struct.pack(">IIIII", created, created, 1, 0, movie_ms),  # ..., track_ID, reserved, duration
        bytes(8),  # reserved
        struct.pack(">hhH", 0, 0, 0x0100),  # layer, alternate_group, volume 1.0
        bytes(2),  # reserved
        _UNITY_MATRIX,
        struct.pack(">II", 0, 0),  # width, height (audio)
    )
    # Edit list trims the final partial AAC frame so the movie duration is exactly the requested one.
    edts = _box(b"edts", _full_box(b"elst", 0, 0, struct.pack(">IIihh", 1, movie_ms, 0, 1, 0)))
    mdhd = _full_box(
        b"mdhd",
        0,
        0,
        struct.pack(">IIII", created, created, sample_rate, media_duration),
        struct.pack(">HH", 0x55C4, 0),  # language 'und', pre_defined
    )
    hdlr = _full_box(b"hdlr", 0, 0, struct.pack(">I4s", 0, b"soun"), bytes(12), b"SoundHandler\x00")
    smhd = _full_box(b"smhd", 0, 0, struct.pack(">hH", 0, 0))  # balance, reserved
    dinf = _box(b"dinf", _full_box(b"dref", 0, 0, struct.pack(">I", 1), _full_box(b"url ", 0, 1)))
    mp4a = _box(
        b"mp4a",
        bytes(6),
        struct.pack(">H", 1),  # data_reference_index
        bytes(8),  # reserved
        struct.pack(">HHHHI", 1, 16, 0, 0, (sample_rate << 16) if sample_rate < 65536 else 0),
        _esds(rate_index, frame_bytes, bitrate),
    )
    stbl = _box(
        b"stbl",
        _full_box(b"stsd", 0, 0, struct.pack(">I", 1), mp4a),
        _full_box(b"stts", 0, 0, struct.pack(">III", 1, n_frames, _AAC_FRAME_SAMPLES)),
        _full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, n_frames, 1)),
        _full_box(b"stsz", 0, 0, struct.pack(">II", frame_bytes, n_frames)),  # constant size, no table
        _full_box(b"stco", 0, 0, struct.pack(">II", 1, mdat_offset)),
    )
    mdia = _box(b"mdia", mdhd, hdlr, _box(b"minf", smhd, dinf, stbl))
    return _box(b"moov", mvhd, _box(b"trak", tkhd, edts, mdia), _ilst_title(title) if title else b"")


def _build_mp4_family(
    *,
    major_brand: bytes,
    minor_version: int,
    compatible_brands: Sequence[bytes],
    duration_s: float,
    seed: int,
    title: str,
    created: datetime | None,
    sample_rate: int,
) -> bytes:
    """Assemble ``ftyp`` + ``moov`` + ``mdat`` (moov first, so HTTP clients can start playback)."""
    if sample_rate not in _AAC_SAMPLE_RATES:
        raise ValueError(f"sample_rate must be one of the AAC rates {_AAC_SAMPLE_RATES}")
    n_samples = round(duration_s * sample_rate)
    if n_samples < 1:
        raise ValueError("duration_s must be at least one sample long")
    n_frames = -(-n_samples // _AAC_FRAME_SAMPLES)  # ceil
    if n_frames * _AAC_FRAME_SAMPLES >= 2**32:
        raise ValueError("duration_s is too long for 32-bit ISO-BMFF durations")
    when = created or _DEFAULT_CREATED
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    created_s = int((when - _MAC_EPOCH).total_seconds())
    if not 0 <= created_s < 2**32:
        raise ValueError("created must fall between 1904 and 2040")

    # The seed only picks global_gain; with zero scalefactor bands every value decodes to silence,
    # but distinct seeds still yield distinct bytes (and content hashes).
    frame = _silent_aac_frame(128 + seed % 128)
    ftyp = _box(b"ftyp", major_brand, struct.pack(">I", minor_version), *compatible_brands)
    params = {
        "created": created_s,
        "movie_ms": max(1, round(duration_s * _MOVIE_TIMESCALE)),
        "n_frames": n_frames,
        "sample_rate": sample_rate,
        "rate_index": _AAC_SAMPLE_RATES.index(sample_rate),
        "frame_bytes": len(frame),
        "title": title,
    }
    moov_size = len(_moov(mdat_offset=0, **params))  # size does not depend on the offset value
    mdat_offset = len(ftyp) + moov_size + 8
    moov = _moov(mdat_offset=mdat_offset, **params)
    return ftyp + moov + _box(b"mdat", frame * n_frames)


def make_m4a(
    duration_s: float,
    *,
    seed: int = 0,
    title: str = "",
    created: datetime | None = None,
    sample_rate: int = _DEFAULT_AUDIO_SAMPLE_RATE,
) -> bytes:
    """Audio-only M4A (brand ``M4A ``): mono AAC-LC, silent, ``duration_s`` long.

    Each 1024-sample frame is a 4-byte raw AAC frame, so cost scales with ``sample_rate``
    (an hour at the default 16 kHz is about 225 KB).  ``created`` fills the mvhd/tkhd/mdhd
    creation times (naive datetimes are treated as UTC); ``title`` becomes the ``(c)nam`` tag.
    """
    return _build_mp4_family(
        major_brand=b"M4A ",
        minor_version=0,
        compatible_brands=(b"M4A ", b"mp42", b"isom"),
        duration_s=duration_s,
        seed=seed,
        title=title,
        created=created,
        sample_rate=sample_rate,
    )


def make_mp4(
    duration_s: float,
    *,
    seed: int = 0,
    title: str = "",
    created: datetime | None = None,
    sample_rate: int = _DEFAULT_AUDIO_SAMPLE_RATE,
) -> bytes:
    """MP4 (major brand ``isom``, also ``iso2``/``mp41``/``mp42``) served as ``video/mp4``.

    The container is the same as :func:`make_m4a`.  The video track is deliberately omitted:
    generating a valid H.264 stream in pure Python is not worth it, and the transcription
    pipelines these recordings feed only extract the audio.
    """
    return _build_mp4_family(
        major_brand=b"isom",
        minor_version=512,
        compatible_brands=(b"isom", b"iso2", b"mp41", b"mp42"),
        duration_s=duration_s,
        seed=seed,
        title=title,
        created=created,
        sample_rate=sample_rate,
    )


# ---------------------------------------------------------------------------------------------
# Transcripts (WebVTT / SRT)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cue:
    """One timed utterance; times are seconds from the start of the recording."""

    start_s: float
    end_s: float
    speaker: str
    text: str

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


_ID_NAMESPACE = uuid.UUID("5d6a9b4e-3c0f-4e7e-9a55-1c2f6d8b7a10")
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_UNESCAPES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ", "&lrm;": "‎", "&rlm;": "‏"}
_ESCAPE_RE = re.compile(r"[&<>]")
_UNESCAPE_RE = re.compile("|".join(_UNESCAPES))
_TIMESTAMP = r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
_TIMING_RE = re.compile(rf"^\s*{_TIMESTAMP}\s+-->\s+{_TIMESTAMP}(?:\s.*)?$")
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]*)*[ \t]+([^>]*)>")
_TAG_RE = re.compile(r"</?[^>\n]*>")
_PLAIN_SPEAKER_RE = re.compile(r"^([^\s:<>][^:\n<>]{0,79}):[ \t]+(.*)$", re.DOTALL)
_NON_CUE_BLOCK_RE = re.compile(r"^(?:NOTE|STYLE|REGION)(?:[ \t]|$)")


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group()], text)


def _unescape(text: str) -> str:
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPES[m.group()], text)


def _clean_text(text: str) -> str:
    """Trim lines and drop blank ones: a blank line would terminate the cue in both formats."""
    lines = (line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


def _to_ms(seconds: float) -> int:
    if seconds < 0:
        raise ValueError(f"negative cue time: {seconds}")
    return round(seconds * 1000)


def _format_ts(seconds: float, sep: str) -> str:
    ms = _to_ms(seconds)
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def _timing_line(cue: Cue, sep: str) -> str:
    if _to_ms(cue.end_s) < _to_ms(cue.start_s):
        raise ValueError(f"cue ends before it starts: {cue}")
    return f"{_format_ts(cue.start_s, sep)} --> {_format_ts(cue.end_s, sep)}"


def make_vtt(
    cues: Sequence[Cue],
    *,
    header_note: str | None = None,
    with_voice_tags: bool = True,
    cue_ids: CueIdStyle = "uuid",
) -> bytes:
    """Render ``cues`` as WebVTT (UTF-8, ``\\n`` line endings).

    ``with_voice_tags=True`` gives the Teams look (``<v Speaker>text</v>``); ``False`` gives the
    Zoom look (``Speaker: text``).  ``cue_ids`` is ``"uuid"`` (Teams: ``<guid>/<n>-0``, the guid being
    a deterministic digest of the content), ``"index"`` (Zoom: 1, 2, 3, ...) or ``"none"``.
    Text is escaped (``&``, ``<``, ``>``) as the WebVTT spec requires.
    """
    if cue_ids not in ("uuid", "index", "none"):
        raise ValueError(f"cue_ids must be 'uuid', 'index' or 'none', not {cue_ids!r}")
    blocks = ["WEBVTT"]
    if header_note and (note := _clean_text(header_note.replace("-->", "->"))):
        blocks.append(f"NOTE {note}" if "\n" not in note else f"NOTE\n{note}")
    document_id = uuid.uuid5(
        _ID_NAMESPACE, "\n".join(f"{c.start_s}|{c.end_s}|{c.speaker}|{c.text}" for c in cues)
    )
    for index, cue in enumerate(cues, start=1):
        lines = []
        if cue_ids == "uuid":
            lines.append(f"{document_id}/{index}-0")
        elif cue_ids == "index":
            lines.append(str(index))
        lines.append(_timing_line(cue, "."))
        text = _escape(_clean_text(cue.text))
        speaker = _escape(" ".join(cue.speaker.split()))
        if speaker and with_voice_tags:
            lines.append(f"<v {speaker}>{text}</v>")
        elif speaker:
            lines.append(f"{speaker}: {text}")
        else:
            lines.append(text)
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def make_srt(cues: Sequence[Cue]) -> bytes:
    """Render ``cues`` as SubRip: 1-based indices, comma milliseconds, ``Speaker: text`` lines."""
    out: list[str] = []
    for index, cue in enumerate(cues, start=1):
        text = _clean_text(cue.text)
        speaker = " ".join(cue.speaker.split())
        out.append(f"{index}\n{_timing_line(cue, ',')}\n{f'{speaker}: {text}' if speaker else text}\n\n")
    return "".join(out).encode("utf-8")


def _parse_ts(match: re.Match[str], offset: int) -> float:
    hours, minutes, seconds, millis = match.groups()[offset : offset + 4]
    # One division of whole milliseconds: yields the float nearest the decimal value, so times
    # written by ``_format_ts`` read back bit-identical.
    return (((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)) / 1000


def parse_vtt(data: bytes) -> list[Cue]:
    """Parse WebVTT (either style produced by :func:`make_vtt`, or real Teams/Zoom exports).

    Handles BOM, CRLF, ``MM:SS.mmm`` timestamps, cue settings, cue ids and NOTE/STYLE/REGION
    blocks.  The speaker comes from a leading ``<v ...>`` tag, else from a ``Name: `` prefix, so a
    speaker-less plain cue whose text happens to start with ``Word: `` is ambiguous by nature.
    Only the first voice tag of a cue is used; other markup tags are stripped.
    """
    lines = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            blocks[-1].append(line)
        elif blocks[-1]:
            blocks.append([])
    blocks = [b for b in blocks if b]
    if not blocks or not re.match(r"WEBVTT(?:[ \t]|$)", blocks[0][0]):
        raise ValueError("not a WebVTT document (missing WEBVTT header)")

    cues: list[Cue] = []
    for block in blocks[1:]:
        if _NON_CUE_BLOCK_RE.match(block[0]):
            continue
        timing_at = next((i for i in (0, 1) if i < len(block) and "-->" in block[i]), None)
        if timing_at is None or not (timing := _TIMING_RE.match(block[timing_at])):
            continue
        payload = "\n".join(block[timing_at + 1 :])
        speaker = ""
        if voice := _VOICE_RE.match(payload):
            speaker, payload = voice.group(1), payload[voice.end() :]
        # Split on the still-escaped text so an escaped "<" or ">" in a name cannot confuse the match.
        text = _TAG_RE.sub("", payload).strip()
        if not speaker.strip() and (plain := _PLAIN_SPEAKER_RE.match(text)):
            speaker, text = plain.groups()
        cues.append(
            Cue(
                _parse_ts(timing, 0),
                _parse_ts(timing, 4),
                _unescape(speaker).strip(),
                _unescape(text).strip(),
            )
        )
    return cues


# ---------------------------------------------------------------------------------------------
# Dialogue -> cues
# ---------------------------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:])\s+")


def estimate_speech_duration(text: str, *, wpm: int = 150) -> float:
    """Seconds needed to speak ``text`` at ``wpm`` words per minute (whitespace-delimited words)."""
    if wpm <= 0:
        raise ValueError("wpm must be positive")
    return len(text.split()) * 60.0 / wpm


def _pieces(text: str, max_words: int) -> list[list[str]]:
    """Split ``text`` (as word lists) at sentence boundaries, then commas, then raw word counts."""
    pieces: list[list[str]] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        for clause in [sentence] if len(sentence.split()) <= max_words else _CLAUSE_SPLIT_RE.split(sentence):
            words = clause.split()
            pieces.extend(words[i : i + max_words] for i in range(0, len(words), max_words))
    return pieces


def build_cues(
    turns: Sequence[tuple[str, str]],
    *,
    start_offset_s: float = 0.0,
    gap_s: float = 0.6,
    wpm: int = 155,
    max_cue_s: float = 8.0,
) -> list[Cue]:
    """Turn ``(speaker, utterance)`` pairs into consecutive timed cues.

    Each cue lasts :func:`estimate_speech_duration` of its text; ``gap_s`` of silence follows
    every turn.  Long utterances are cut at sentence boundaries, then commas, then (as a last
    resort) mid-sentence, and the pieces are greedily repacked so no cue exceeds ``max_cue_s``
    (a single word longer than that is clamped to it).  Times are whole milliseconds, so the
    result survives a VTT/SRT round trip unchanged.
    """
    if max_cue_s <= 0 or wpm <= 0:
        raise ValueError("max_cue_s and wpm must be positive")
    max_words = max(1, math.floor(max_cue_s * wpm / 60 + 1e-9))
    max_ms = round(max_cue_s * 1000)
    gap_ms = round(gap_s * 1000)
    clock_ms = round(start_offset_s * 1000)
    cues: list[Cue] = []
    for speaker, utterance in turns:
        packed: list[list[str]] = []
        for piece in _pieces(_clean_text(utterance).replace("\n", " "), max_words):
            if packed and len(packed[-1]) + len(piece) <= max_words:
                packed[-1].extend(piece)
            else:
                packed.append(list(piece))
        for words in packed:
            text = " ".join(words)
            length_ms = min(max_ms, round(estimate_speech_duration(text, wpm=wpm) * 1000))
            cues.append(Cue(clock_ms / 1000, (clock_ms + length_ms) / 1000, speaker, text))
            clock_ms += length_ms
        if packed:
            clock_ms += gap_ms
    return cues
