"""The synthetic media must be real files: parsed here by a small ISO-BMFF walker, the stdlib
``wave`` module and (when installed) ffprobe/ffmpeg/mutagen, not just compared to golden bytes."""

from __future__ import annotations

import io
import itertools
import json
import math
import re
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from emulators.world import media
from emulators.world.media import Cue

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
needs_ffprobe = pytest.mark.skipif(FFPROBE is None, reason="ffprobe not on PATH")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")

MAC_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


# ---------------------------------------------------------------------------------------------
# Minimal ISO-BMFF reader (deliberately independent of the writer in media.py)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Box:
    kind: str
    start: int  # offset of the size field
    header: int  # 8, or 16 with a 64-bit largesize
    size: int
    data: bytes  # the whole file

    @property
    def body_start(self) -> int:
        return self.start + self.header

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def body(self) -> bytes:
        return self.data[self.body_start : self.end]


def walk(data: bytes, start: int = 0, end: int | None = None) -> list[Box]:
    """Parse sibling boxes in ``[start, end)``; they must tile the range exactly."""
    end = len(data) if end is None else end
    boxes: list[Box] = []
    pos = start
    while pos < end:
        assert end - pos >= 8, f"truncated box header at {pos}"
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            header = 16
        elif size == 0:
            size = end - pos
        assert header <= size <= end - pos, f"box {kind!r} at {pos} overruns its parent"
        boxes.append(Box(kind.decode("latin-1"), pos, header, size, data))
        pos += size
    assert pos == end
    return boxes


# Bytes between a box's header and its first child box.
CHILD_SKIP = {"meta": 4, "stsd": 8, "dref": 8, "mp4a": 28}
CONTAINERS = {"moov", "trak", "edts", "mdia", "minf", "dinf", "stbl", "udta", "ilst", *CHILD_SKIP}


def children(box: Box) -> list[Box]:
    assert box.kind in CONTAINERS, f"{box.kind} is not a container"
    return walk(box.data, box.body_start + CHILD_SKIP.get(box.kind, 0), box.end)


def descend(data: bytes, path: str) -> Box:
    """Find the one box at ``path`` (e.g. ``moov/trak/mdia/mdhd``); each step must be unambiguous."""
    boxes = walk(data)
    box: Box | None = None
    for kind in path.split("/"):
        matches = [b for b in boxes if b.kind == kind]
        assert len(matches) == 1, f"expected exactly one {kind!r} under {path!r}, got {len(matches)}"
        box = matches[0]
        boxes = children(box) if box.kind in CONTAINERS else []
    assert box is not None
    return box


def has(data: bytes, path: str) -> bool:
    try:
        descend(data, path)
    except AssertionError:
        return False
    return True


def full_body(box: Box) -> tuple[int, int, bytes]:
    """(version, flags, rest) of a FullBox."""
    (word,) = struct.unpack_from(">I", box.data, box.body_start)
    return word >> 24, word & 0xFFFFFF, box.data[box.body_start + 4 : box.end]


def descriptors(buf: bytes) -> list[tuple[int, bytes]]:
    """Parse MPEG-4 systems descriptors (tag, 1-4 byte expandable length, body)."""
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(buf):
        tag = buf[pos]
        pos += 1
        length = 0
        for _ in range(4):
            byte = buf[pos]
            pos += 1
            length = (length << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        out.append((tag, buf[pos : pos + length]))
        pos += length
    return out


class Bits:
    """MSB-first bit reader."""

    def __init__(self, data: bytes) -> None:
        self.value = int.from_bytes(data, "big")
        self.total = len(data) * 8
        self.pos = 0

    def read(self, count: int) -> int:
        shift = self.total - self.pos - count
        assert shift >= 0, "read past the end"
        self.pos += count
        return (self.value >> shift) & ((1 << count) - 1)


@dataclass
class Mp4:
    data: bytes
    ftyp_major: str
    ftyp_minor: int
    ftyp_compat: list[str]
    mvhd: dict[str, int]
    tkhd: dict[str, int]
    elst: list[tuple[int, int]]
    mdhd: dict[str, int]
    stts: list[tuple[int, int]]
    stsc: list[tuple[int, int, int]]
    stsz: tuple[int, int, list[int]]
    stco: list[int]
    mdat: Box
    asc: dict[str, int]
    es: dict[str, int]


def parse_mp4(data: bytes) -> Mp4:
    ftyp = descend(data, "ftyp")
    major, minor = struct.unpack_from(">4sI", ftyp.body)
    compat_raw = ftyp.body[8:]
    compat = [compat_raw[i : i + 4].decode("latin-1") for i in range(0, len(compat_raw), 4)]

    _, _, mvhd_rest = full_body(descend(data, "moov/mvhd"))
    created, modified, timescale, duration, rate, volume = struct.unpack_from(">IIIIIH", mvhd_rest)
    (next_track_id,) = struct.unpack_from(">I", mvhd_rest, len(mvhd_rest) - 4)
    assert len(mvhd_rest) == 96  # version 0 layout: nothing missing, nothing extra
    mvhd = {
        "created": created,
        "modified": modified,
        "timescale": timescale,
        "duration": duration,
        "rate": rate,
        "volume": volume,
        "next_track_id": next_track_id,
    }

    _, tkhd_flags, tkhd_rest = full_body(descend(data, "moov/trak/tkhd"))
    t_created, t_modified, track_id, _, t_duration = struct.unpack_from(">IIIII", tkhd_rest)
    tkhd = {
        "flags": tkhd_flags,
        "created": t_created,
        "modified": t_modified,
        "track_id": track_id,
        "duration": t_duration,
    }

    _, _, elst_rest = full_body(descend(data, "moov/trak/edts/elst"))
    (count,) = struct.unpack_from(">I", elst_rest)
    assert len(elst_rest) == 4 + 12 * count
    elst = [struct.unpack_from(">Ii", elst_rest, 4 + 12 * i) for i in range(count)]
    assert all(struct.unpack_from(">hh", elst_rest, 12 + 12 * i) == (1, 0) for i in range(count))  # rate 1.0

    _, _, mdhd_rest = full_body(descend(data, "moov/trak/mdia/mdhd"))
    m_created, m_modified, m_scale, m_duration = struct.unpack_from(">IIII", mdhd_rest)
    mdhd = {"created": m_created, "modified": m_modified, "timescale": m_scale, "duration": m_duration}

    stbl = "moov/trak/mdia/minf/stbl/"
    _, _, rest = full_body(descend(data, stbl + "stts"))
    (count,) = struct.unpack_from(">I", rest)
    stts = [struct.unpack_from(">II", rest, 4 + 8 * i) for i in range(count)]

    _, _, rest = full_body(descend(data, stbl + "stsc"))
    (count,) = struct.unpack_from(">I", rest)
    stsc = [struct.unpack_from(">III", rest, 4 + 12 * i) for i in range(count)]

    _, _, rest = full_body(descend(data, stbl + "stsz"))
    sample_size, sample_count = struct.unpack_from(">II", rest)
    sizes = list(struct.unpack_from(f">{sample_count}I", rest, 8)) if sample_size == 0 else []
    assert len(rest) == 8 + 4 * len(sizes), "stsz has trailing bytes or a missing table"

    _, _, rest = full_body(descend(data, stbl + "stco"))
    (count,) = struct.unpack_from(">I", rest)
    assert len(rest) == 4 + 4 * count
    stco = list(struct.unpack_from(f">{count}I", rest, 4))

    # stsd -> mp4a sample entry -> esds
    mp4a = descend(data, stbl + "stsd/mp4a")
    channels, sample_bits, _, _, rate_fixed = struct.unpack_from(">HHHHI", mp4a.body, 16)
    (esds,) = [b for b in children(mp4a) if b.kind == "esds"]
    _, _, esds_rest = full_body(esds)
    (tag, es_body), *trailing = descriptors(esds_rest)
    assert tag == 0x03 and not trailing
    es_id, es_flags = struct.unpack_from(">HB", es_body)
    assert es_flags == 0
    inner = dict(descriptors(es_body[3:]))
    assert set(inner) == {0x04, 0x06}, "ES_Descriptor must hold DecoderConfig and SLConfig descriptors"
    assert inner[0x06] == b"\x02"
    dc = inner[0x04]
    object_type, stream_type, _bufsize_hi, _bufsize_lo, max_bitrate, avg_bitrate = struct.unpack_from(
        ">BBBHII", dc
    )
    ((spec_tag, asc_bytes),) = descriptors(dc[13:])
    assert spec_tag == 0x05 and len(asc_bytes) == 2
    bits = Bits(asc_bytes)
    asc = {
        "object_type": bits.read(5),
        "rate_index": bits.read(4),
        "channels": bits.read(4),
        "frame_length_flag": bits.read(1),
        "depends_on_core_coder": bits.read(1),
        "extension_flag": bits.read(1),
    }
    es = {
        "es_id": es_id,
        "object_type_indication": object_type,
        "stream_type": stream_type,
        "max_bitrate": max_bitrate,
        "avg_bitrate": avg_bitrate,
        "entry_channels": channels,
        "entry_sample_bits": sample_bits,
        "entry_rate": rate_fixed >> 16,
    }
    return Mp4(
        data=data,
        ftyp_major=major.decode("latin-1"),
        ftyp_minor=minor,
        ftyp_compat=compat,
        mvhd=mvhd,
        tkhd=tkhd,
        elst=elst,
        mdhd=mdhd,
        stts=stts,
        stsc=stsc,
        stsz=(sample_size, sample_count, sizes),
        stco=stco,
        mdat=descend(data, "mdat"),
        asc=asc,
        es=es,
    )


def mac_seconds(moment: datetime) -> int:
    return int((moment - MAC_EPOCH).total_seconds())


# ---------------------------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------------------------


def read_wav(data: bytes) -> tuple[Any, tuple[int, ...]]:
    with wave.open(io.BytesIO(data), "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    return params, struct.unpack(f"<{len(frames) // 2}h", frames)


@pytest.mark.parametrize(
    ("duration", "rate"), [(0.0, 16000), (0.5, 16000), (3.25, 8000), (2.0, 44100), (10.0, 16000)]
)
@pytest.mark.parametrize("tone", [True, False])
def test_wav_is_valid_mono_pcm(duration: float, rate: int, tone: bool) -> None:
    data = media.make_wav(duration, sample_rate=rate, tone=tone)
    params, samples = read_wav(data)
    assert (params.nchannels, params.sampwidth, params.framerate, params.comptype) == (1, 2, rate, "NONE")
    assert params.nframes == round(duration * rate) == len(samples)
    assert params.nframes / params.framerate == pytest.approx(duration, abs=1 / rate)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert struct.unpack_from("<I", data, 4)[0] == len(data) - 8  # RIFF size is correct


def test_wav_tone_is_quiet_speech_like_activity() -> None:
    _, samples = read_wav(media.make_wav(20.0))
    window = 320  # 20 ms at 16 kHz
    rms = [
        math.sqrt(sum(s * s for s in samples[i : i + window]) / window)
        for i in range(0, len(samples) - window + 1, window)
    ]
    peak = max(abs(s) for s in samples)
    assert 0.05 * 32767 < peak < 0.3 * 32767  # audible to an energy VAD, nowhere near clipping
    assert sum(r > 200 for r in rms) / len(rms) > 0.75  # mostly voiced, with some pauses
    assert any(r == 0 for r in rms)  # but not a constant drone
    assert abs(sum(samples) / len(samples)) < 50  # no DC offset


def test_wav_silence_is_all_zero() -> None:
    _, samples = read_wav(media.make_wav(1.0, tone=False))
    assert not any(samples)


def test_wav_is_deterministic_and_seeded() -> None:
    assert media.make_wav(2.0, seed=4) == media.make_wav(2.0, seed=4)
    assert media.make_wav(2.0, seed=4) != media.make_wav(2.0, seed=5)
    assert media.make_wav(2.0, seed=-3) == media.make_wav(2.0, seed=-3)


def test_wav_prefix_is_stable_across_durations() -> None:
    """Longer recordings of one seed extend the shorter ones (no rescaling artefacts)."""
    _, short = read_wav(media.make_wav(1.0, seed=2))
    _, long = read_wav(media.make_wav(3.0, seed=2))
    assert long[: len(short)] == short


def test_wav_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="duration_s"):
        media.make_wav(-1.0)
    with pytest.raises(ValueError, match="sample_rate"):
        media.make_wav(1.0, sample_rate=0)


# ---------------------------------------------------------------------------------------------
# M4A / MP4
# ---------------------------------------------------------------------------------------------

DURATIONS = [0.01, 1.0, 12.5, 61.234, 3600.0]


def build(kind: str, duration: float, **kwargs: Any) -> bytes:
    return (media.make_m4a if kind == "m4a" else media.make_mp4)(duration, **kwargs)


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
@pytest.mark.parametrize("duration", DURATIONS)
def test_box_tree_and_timing_are_consistent(kind: str, duration: float) -> None:
    data = build(kind, duration)
    rate = 16000

    # Top level: ftyp, moov, mdat tile the file exactly (walk() asserts that).
    assert [b.kind for b in walk(data)] == ["ftyp", "moov", "mdat"]
    moov = descend(data, "moov")
    assert [b.kind for b in children(moov)] == ["mvhd", "trak"]
    trak = descend(data, "moov/trak")
    assert [b.kind for b in children(trak)] == ["tkhd", "edts", "mdia"]
    mdia = descend(data, "moov/trak/mdia")
    assert [b.kind for b in children(mdia)] == ["mdhd", "hdlr", "minf"]
    assert [b.kind for b in children(descend(data, "moov/trak/mdia/minf"))] == ["smhd", "dinf", "stbl"]
    stbl = descend(data, "moov/trak/mdia/minf/stbl")
    assert [b.kind for b in children(stbl)] == ["stsd", "stts", "stsc", "stsz", "stco"]

    _, _, hdlr = full_body(descend(data, "moov/trak/mdia/hdlr"))
    assert hdlr[4:8] == b"soun"
    assert hdlr.endswith(b"\x00")
    _, _, dref = full_body(descend(data, "moov/trak/mdia/minf/dinf/dref"))
    assert struct.unpack_from(">I", dref)[0] == 1
    (url_box,) = children(descend(data, "moov/trak/mdia/minf/dinf/dref"))
    assert url_box.kind == "url " and full_body(url_box)[1] == 1  # self-contained media
    stsd = descend(data, "moov/trak/mdia/minf/stbl/stsd")
    assert [b.kind for b in children(stsd)] == ["mp4a"]
    assert [b.kind for b in children(descend(data, "moov/trak/mdia/minf/stbl/stsd/mp4a"))] == ["esds"]

    m = parse_mp4(data)
    n_frames = math.ceil(duration * rate / 1024)

    # Sample tables.
    sample_size, sample_count, sizes = m.stsz
    assert sample_count == n_frames
    assert m.stts == [(n_frames, 1024)]
    assert m.stsc == [(1, n_frames, 1)]  # one chunk holding every sample, sample description 1
    assert sample_size == 4 and not sizes

    # Timescales and durations agree with each other and with the requested length.
    assert m.mdhd["timescale"] == rate == m.es["entry_rate"]
    assert m.mdhd["duration"] == sum(count * delta for count, delta in m.stts) == n_frames * 1024
    assert 0 <= m.mdhd["duration"] / rate - duration < 1024 / rate
    assert m.mvhd["timescale"] == 1000
    assert m.mvhd["duration"] == m.tkhd["duration"] == round(duration * 1000)
    assert m.elst == [(round(duration * 1000), 0)]
    assert m.tkhd["track_id"] == 1 and m.tkhd["flags"] & 0b11 == 0b11  # enabled, in movie
    assert m.mvhd["next_track_id"] == 2 and m.mvhd["rate"] == 0x00010000 and m.mvhd["volume"] == 0x0100

    # Chunk offset points at the first byte of the mdat payload, and the samples fit inside it.
    (offset,) = m.stco
    assert m.mdat.body_start <= offset < m.mdat.end
    assert offset == m.mdat.body_start
    assert offset + sample_size * sample_count == m.mdat.end


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
@pytest.mark.parametrize("rate", AAC_RATES)
def test_esds_audio_specific_config_is_aac_lc(kind: str, rate: int) -> None:
    m = parse_mp4(build(kind, 2.0, sample_rate=rate))
    assert m.asc == {
        "object_type": 2,  # AAC LC
        "rate_index": AAC_RATES.index(rate),
        "channels": 1,
        "frame_length_flag": 0,  # 1024-sample frames
        "depends_on_core_coder": 0,
        "extension_flag": 0,
    }
    assert m.es["object_type_indication"] == 0x40  # MPEG-4 audio
    assert m.es["stream_type"] == 0x15  # audio stream, reserved bit set
    assert (m.es["entry_channels"], m.es["entry_sample_bits"]) == (1, 16)
    assert m.mdhd["timescale"] == rate
    assert m.stsz[1] == math.ceil(2.0 * rate / 1024)
    assert m.es["avg_bitrate"] == m.es["max_bitrate"] == round(4 * 8 * rate / 1024)


def test_default_rate_asc_bytes() -> None:
    """Known answer: AAC-LC, 16 kHz, mono is the two bytes 14 08."""
    data = media.make_m4a(1.0)
    # DecoderSpecificInfo descriptor (tag 5, 4-byte length form, 2 bytes) followed by SLConfig.
    assert bytes.fromhex("0580808002140806808080 0102".replace(" ", "")) in data


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
def test_every_sample_is_a_wellformed_silent_aac_lc_frame(kind: str) -> None:
    m = parse_mp4(build(kind, 5.0, seed=9))
    payload = m.data[m.mdat.body_start : m.mdat.end]
    sample_size = m.stsz[0]
    frames = {payload[i : i + sample_size] for i in range(0, len(payload), sample_size)}
    assert len(payload) % sample_size == 0 and len(frames) == 1
    bits = Bits(next(iter(frames)))
    assert bits.read(3) == 0  # ID_SCE
    assert bits.read(4) == 0  # element_instance_tag
    assert bits.read(8) == 128 + 9  # global_gain, derived from the seed
    assert bits.read(1) == 0  # ics_reserved_bit
    assert bits.read(2) == 0  # ONLY_LONG_SEQUENCE
    assert bits.read(1) == 1  # window_shape
    assert bits.read(6) == 0  # max_sfb: no scalefactor bands, hence no spectral data
    assert [bits.read(1) for _ in range(4)] == [0, 0, 0, 0]  # predictor, pulse, tns, gain control
    assert bits.read(3) == 0b111  # ID_END
    assert bits.pos == bits.total == 32  # byte aligned with nothing left over


def test_seed_32_gives_the_classic_silence_frame() -> None:
    m = parse_mp4(media.make_m4a(1.0, seed=32))
    assert m.data[m.mdat.body_start : m.mdat.body_start + 4] == bytes.fromhex("01402007")


@pytest.mark.parametrize(
    "created",
    [
        datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC),
        datetime(2025, 3, 4, 5, 6, 7),  # naive means UTC
        datetime(2025, 3, 4, 7, 6, 7, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_creation_time(created: datetime) -> None:
    m = parse_mp4(media.make_m4a(3.0, created=created))
    expected = mac_seconds(datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC))
    assert m.mvhd["created"] == m.mvhd["modified"] == expected
    assert m.tkhd["created"] == m.tkhd["modified"] == expected
    assert m.mdhd["created"] == m.mdhd["modified"] == expected


def test_default_creation_time_is_fixed() -> None:
    a, b = parse_mp4(media.make_m4a(1.0)), parse_mp4(media.make_m4a(1.0))
    assert a.mvhd["created"] == b.mvhd["created"] == mac_seconds(datetime(2024, 1, 1, tzinfo=UTC))


def test_creation_time_out_of_range() -> None:
    with pytest.raises(ValueError, match="created"):
        media.make_m4a(1.0, created=datetime(1900, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="created"):
        media.make_m4a(1.0, created=datetime(2050, 1, 1, tzinfo=UTC))


def test_brands() -> None:
    m4a = parse_mp4(media.make_m4a(1.0))
    assert m4a.ftyp_major == "M4A " and "M4A " in m4a.ftyp_compat and "isom" in m4a.ftyp_compat
    mp4 = parse_mp4(media.make_mp4(1.0))
    assert mp4.ftyp_major == "isom" and {"isom", "mp42"} <= set(mp4.ftyp_compat)


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
def test_title_tag(kind: str) -> None:
    title = "Weekly sync – café ✓"
    data = build(kind, 2.0, title=title)
    parse_mp4(data)  # still structurally sound
    meta = descend(data, "moov/udta/meta")
    assert [b.kind for b in children(meta)] == ["hdlr", "ilst"]
    _, _, hdlr = full_body(children(meta)[0])
    assert hdlr[4:8] == b"mdir"
    nam = descend(data, "moov/udta/meta/ilst/\xa9nam")
    (data_box,) = walk(data, nam.body_start, nam.end)
    assert data_box.kind == "data"
    type_flags, locale = struct.unpack_from(">II", data_box.body)
    assert (type_flags, locale) == (1, 0)  # UTF-8 text
    assert data_box.body[8:].decode("utf-8") == title
    assert data_box.end == nam.end


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
def test_no_udta_without_title(kind: str) -> None:
    assert not has(build(kind, 2.0), "moov/udta")


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
def test_title_does_not_break_chunk_offset(kind: str) -> None:
    m = parse_mp4(build(kind, 7.0, title="x" * 300))
    assert m.stco == [m.mdat.body_start]


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
def test_deterministic_and_seed_sensitive(kind: str) -> None:
    assert build(kind, 30.0, seed=3, title="t") == build(kind, 30.0, seed=3, title="t")
    assert build(kind, 30.0, seed=3) != build(kind, 30.0, seed=4)
    assert build(kind, 30.0) != build(kind, 31.0)
    assert media.make_m4a(5.0) != media.make_mp4(5.0)


def test_hour_long_file_stays_small() -> None:
    data = media.make_m4a(3600.0)
    assert len(data) < 250_000
    assert len(media.make_m4a(3600.0, sample_rate=48000)) < 700_000
    assert len(media.make_m4a(1.0)) < 1000  # container overhead is tiny


@pytest.mark.parametrize("kind", ["m4a", "mp4"])
def test_rejects_bad_arguments(kind: str) -> None:
    with pytest.raises(ValueError, match="duration_s"):
        build(kind, 0.0)
    with pytest.raises(ValueError, match="duration_s"):
        build(kind, -1.0)
    with pytest.raises(ValueError, match="sample_rate"):
        build(kind, 1.0, sample_rate=12345)


def test_mime_constants() -> None:
    assert media.MIME_WAV == "audio/wav"
    assert media.MIME_M4A == "audio/mp4"
    assert media.MIME_MP4 == "video/mp4"
    assert media.MIME_VTT == "text/vtt"
    assert media.MIME_SRT == "application/x-subrip"


# --- real parsers -----------------------------------------------------------------------------


def run(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=120, check=False)


def ffprobe_json(path: Path) -> dict:
    assert FFPROBE
    result = run([FFPROBE, "-v", "warning", "-show_format", "-show_streams", "-of", "json", str(path)])
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stderr == b"", f"ffprobe warnings: {result.stderr.decode(errors='replace')}"
    return json.loads(result.stdout)


@needs_ffprobe
@pytest.mark.parametrize("kind", ["m4a", "mp4"])
@pytest.mark.parametrize("duration", [0.5, 12.5, 90.0, 3600.0])
@pytest.mark.parametrize("rate", [16000, 48000])
def test_ffprobe_reports_aac(tmp_path: Path, kind: str, duration: float, rate: int) -> None:
    created = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
    path = tmp_path / f"rec.{kind}"
    path.write_bytes(build(kind, duration, title="Standup", created=created, sample_rate=rate))

    info = ffprobe_json(path)
    (stream,) = info["streams"]
    assert stream["codec_type"] == "audio"
    assert stream["codec_name"] == "aac"
    assert stream["profile"] == "LC"
    assert stream["channels"] == 1
    assert int(stream["sample_rate"]) == rate
    assert float(stream["duration"]) == pytest.approx(duration, abs=0.1)
    assert float(info["format"]["duration"]) == pytest.approx(duration, abs=0.1)
    assert "mp4" in info["format"]["format_name"].split(",")
    tags = info["format"]["tags"]
    assert tags["major_brand"] == ("M4A " if kind == "m4a" else "isom")
    assert tags["title"] == "Standup"
    assert tags["creation_time"].startswith("2025-03-04T05:06:07")


@needs_ffmpeg
@pytest.mark.parametrize("kind", ["m4a", "mp4"])
@pytest.mark.parametrize("seed", [0, 1, 32, 127, 12345])
def test_ffmpeg_decodes_without_errors_to_silence(tmp_path: Path, kind: str, seed: int) -> None:
    assert FFMPEG
    path = tmp_path / f"rec.{kind}"
    path.write_bytes(build(kind, 7.3, seed=seed))

    null = run([FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"])
    assert null.returncode == 0
    assert null.stderr == b"", null.stderr.decode(errors="replace")

    pcm = run([FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le", "-"])
    assert pcm.returncode == 0 and pcm.stderr == b""
    assert abs(len(pcm.stdout) // 2 - 7.3 * 16000) <= 1024  # whole AAC frames
    assert not any(pcm.stdout)  # decoded audio is digital silence


def test_mutagen_reads_m4a(tmp_path: Path) -> None:
    mp4 = pytest.importorskip("mutagen.mp4")
    path = tmp_path / "rec.m4a"
    path.write_bytes(media.make_m4a(42.0, title="Retro"))
    tagged = mp4.MP4(path)
    assert tagged.info.length == pytest.approx(42.0, abs=0.1)
    assert tagged.info.channels == 1
    assert tagged.info.sample_rate == 16000
    assert tagged.info.codec == "mp4a.40.2"
    assert tagged.tags["\xa9nam"] == ["Retro"]


# ---------------------------------------------------------------------------------------------
# WebVTT / SRT
# ---------------------------------------------------------------------------------------------

CUES = [
    Cue(1.28, 4.56, "Alice Smith", "Hello everyone, thanks for joining."),
    Cue(5.0, 7.25, "Bob Jones", "Happy to be here & ready <to> go."),
    Cue(3723.5, 3725.001, "Carol O'Neil (Acme)", "First line\nsecond line"),
    Cue(360000.0, 360001.5, "Dana", "Very late cue."),
]
UUID_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/(\d+)-0$")
TIMING = re.compile(r"^\d{2,}:\d{2}:\d{2}\.\d{3} --> \d{2,}:\d{2}:\d{2}\.\d{3}$")


def cue_blocks(vtt: bytes) -> list[list[str]]:
    text = vtt.decode("utf-8")
    assert text.startswith("WEBVTT\n") and text.endswith("\n") and "\r" not in text
    return [block.split("\n") for block in text.strip("\n").split("\n\n")][1:]


ID_STYLES: list[media.CueIdStyle] = ["uuid", "index", "none"]


@pytest.mark.parametrize("cue_ids", ID_STYLES)
@pytest.mark.parametrize("voice_tags", [True, False])
@pytest.mark.parametrize("note", [None, "Recording of weekly sync"])
def test_vtt_round_trip(cue_ids: media.CueIdStyle, voice_tags: bool, note: str | None) -> None:
    data = media.make_vtt(CUES, header_note=note, with_voice_tags=voice_tags, cue_ids=cue_ids)
    assert media.parse_vtt(data) == CUES


def test_vtt_teams_style_layout() -> None:
    data = media.make_vtt(CUES)  # defaults: voice tags + uuid ids
    blocks = cue_blocks(data)
    assert len(blocks) == len(CUES)
    guids = set()
    for n, (block, cue) in enumerate(zip(blocks, CUES, strict=True), start=1):
        match = UUID_ID.match(block[0])
        assert match and int(match.group(1)) == n
        guids.add(block[0].split("/")[0])
        assert TIMING.match(block[1])
        assert block[2].startswith(f"<v {cue.speaker}>") and block[-1].endswith("</v>")
    assert len(guids) == 1  # one GUID per document, unique "/n-0" suffix per cue
    assert blocks[0][1] == "00:00:01.280 --> 00:00:04.560"
    assert blocks[2][1] == "01:02:03.500 --> 01:02:05.001"
    assert blocks[3][1] == "100:00:00.000 --> 100:00:01.500"
    assert blocks[1][2] == "<v Bob Jones>Happy to be here &amp; ready &lt;to&gt; go.</v>"
    assert blocks[2][2:] == ["<v Carol O'Neil (Acme)>First line", "second line</v>"]


def test_vtt_zoom_style_layout_exact() -> None:
    cues = [Cue(1.36, 3.24, "Alice Smith", "Hello everyone."), Cue(3.9, 6.0, "Bob", "Hi, Alice.")]
    data = media.make_vtt(cues, with_voice_tags=False, cue_ids="index", header_note="Zoom export")
    assert data == (
        b"WEBVTT\n\nNOTE Zoom export\n\n"
        b"1\n00:00:01.360 --> 00:00:03.240\nAlice Smith: Hello everyone.\n\n"
        b"2\n00:00:03.900 --> 00:00:06.000\nBob: Hi, Alice.\n"
    )


def test_vtt_cue_ids_none_has_no_id_line() -> None:
    blocks = cue_blocks(media.make_vtt(CUES[:2], cue_ids="none"))
    assert all(TIMING.match(block[0]) for block in blocks)


def test_vtt_multiline_note_and_sanitising() -> None:
    data = media.make_vtt(CUES[:1], header_note="line one\n\nline two --> arrow")
    text = data.decode()
    assert text.startswith("WEBVTT\n\nNOTE\nline one\nline two -> arrow\n\n")
    assert media.parse_vtt(data) == CUES[:1]


def test_vtt_empty_and_speakerless() -> None:
    assert media.make_vtt([]) == b"WEBVTT\n"
    assert media.parse_vtt(media.make_vtt([])) == []
    cue = Cue(0.0, 1.0, "", "Just text")
    for tags in (True, False):
        assert media.parse_vtt(media.make_vtt([cue], with_voice_tags=tags)) == [cue]


def test_vtt_is_deterministic() -> None:
    assert media.make_vtt(CUES) == media.make_vtt(list(CUES))
    assert media.make_vtt(CUES) != media.make_vtt(CUES[:2])  # document GUID follows the content


def test_vtt_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="cue_ids"):
        media.make_vtt(CUES, cue_ids="guid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        media.make_vtt([Cue(-1.0, 1.0, "A", "x")])
    with pytest.raises(ValueError, match="before"):
        media.make_vtt([Cue(2.0, 1.0, "A", "x")])
    with pytest.raises(ValueError, match="WEBVTT"):
        media.parse_vtt(b"1\n00:00:01.000 --> 00:00:02.000\nhi\n")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.0, "00:00:00.000"), (59.9996, "00:01:00.000"), (3661.5, "01:01:01.500"), (7 * 3600, "07:00:00.000")],
)
def test_vtt_timestamp_formatting(seconds: float, text: str) -> None:
    out = media.make_vtt([Cue(seconds, seconds + 1, "A", "x")], cue_ids="none").decode()
    assert out.splitlines()[2].startswith(text + " -->")


def test_parse_vtt_tolerates_real_world_exports() -> None:
    raw = (
        "﻿WEBVTT - Teams\r\nKind: captions\r\n\r\n"
        "NOTE a comment\r\nover two lines\r\n\r\n"
        "STYLE\r\n::cue { color: red }\r\n\r\n"
        "REGION\r\nid:r1\r\n\r\n"
        "1\r\n00:00:01.000 --> 00:00:02.500 align:start position:0%\r\n<v.loud Alice>Hi <i>there</i></v>\r\n\r\n"
        "00:03.000 --> 00:04.000\r\nplain text\r\n\r\n"
        "x-1/2-0\r\n100:00:00.000 --> 100:00:01.000\r\nBob: late &amp; loud &lt;3\r\n\r\n"
        "orphan block without timing\r\n\r\n"
    )
    assert media.parse_vtt(raw.encode("utf-8")) == [
        Cue(1.0, 2.5, "Alice", "Hi there"),
        Cue(3.0, 4.0, "", "plain text"),
        Cue(360000.0, 360001.0, "Bob", "late & loud <3"),
    ]


def test_srt_layout() -> None:
    cues = [Cue(1.28, 4.56, "Alice Smith", "Hello."), Cue(3723.5, 3725.0, "", "Text only\n\nafter blank")]
    data = media.make_srt(cues)
    assert data == (
        b"1\n00:00:01,280 --> 00:00:04,560\nAlice Smith: Hello.\n\n"
        b"2\n01:02:03,500 --> 01:02:05,000\nText only\nafter blank\n\n"
    )
    assert media.make_srt([]) == b""
    # Re-parse structurally the way an SRT consumer would.
    blocks = [b.split("\n") for b in data.decode().strip("\n").split("\n\n")]
    assert [b[0] for b in blocks] == ["1", "2"]
    assert all(re.fullmatch(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", b[1]) for b in blocks)


# --- estimate_speech_duration / build_cues ----------------------------------------------------


def test_estimate_speech_duration() -> None:
    assert media.estimate_speech_duration(" ".join(["word"] * 150)) == pytest.approx(60.0)
    assert media.estimate_speech_duration(" ".join(["word"] * 150), wpm=300) == pytest.approx(30.0)
    assert media.estimate_speech_duration("") == 0.0
    assert media.estimate_speech_duration("  one\ttwo\nthree ", wpm=60) == pytest.approx(3.0)
    with pytest.raises(ValueError, match="wpm"):
        media.estimate_speech_duration("x", wpm=0)


TURNS = [
    (
        "Alice Smith",
        "Thanks everyone for joining today. We have a packed agenda, starting with the quarterly roadmap "
        "review, then moving on to hiring, budget, and finally a few open questions from the floor. "
        "Let's get started.",
    ),
    ("Bob Jones", "Sounds good."),
    ("Carol", "an enormously long run on sentence with no punctuation at all " * 5),
    ("Alice Smith", "Great! Over to you, Carol."),
]


def normalised(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("max_cue_s", [0.2, 1.0, 2.5, 4.0, 8.0, 30.0])
@pytest.mark.parametrize("wpm", [120, 155, 200])
def test_build_cues_never_exceed_max_and_keep_all_text(max_cue_s: float, wpm: int) -> None:
    cues = media.build_cues(TURNS, wpm=wpm, max_cue_s=max_cue_s)
    assert cues
    assert all(0 < c.duration_s <= max_cue_s + 1e-9 for c in cues)
    assert " ".join(c.text for c in cues) == " ".join(normalised(u) for _, u in TURNS)
    assert [speaker for speaker, _ in itertools.groupby(c.speaker for c in cues)] == [s for s, _ in TURNS]
    for cue in cues:
        # Unclamped cues last exactly as long as their words take to say.
        if cue.duration_s < max_cue_s - 0.01:
            assert cue.duration_s == pytest.approx(
                media.estimate_speech_duration(cue.text, wpm=wpm), abs=0.001
            )


def test_build_cues_timing_offset_and_gaps() -> None:
    cues = media.build_cues(TURNS, start_offset_s=12.5, gap_s=0.6)
    assert cues[0].start_s == 12.5
    for prev, cur in itertools.pairwise(cues):
        gap = cur.start_s - prev.end_s
        # Same speaker continuing one utterance: back to back; new turn: exactly gap_s of silence.
        assert gap == pytest.approx(0.0 if prev.speaker == cur.speaker else 0.6, abs=1e-9)
    assert all(c.start_s < c.end_s for c in cues)


def test_build_cues_zero_gap_and_no_turns() -> None:
    assert media.build_cues([]) == []
    cues = media.build_cues([("A", "one two three"), ("B", "four five")], gap_s=0.0, wpm=60)
    assert cues == [Cue(0.0, 3.0, "A", "one two three"), Cue(3.0, 5.0, "B", "four five")]


def test_build_cues_skips_empty_utterances_without_a_gap() -> None:
    cues = media.build_cues([("A", "one"), ("B", "   "), ("C", ""), ("D", "two")], wpm=60, gap_s=1.0)
    assert cues == [Cue(0.0, 1.0, "A", "one"), Cue(2.0, 3.0, "D", "two")]


def test_build_cues_prefers_sentence_boundaries() -> None:
    text = "One two three. Four five six. Seven eight nine."
    # wpm=60 makes every word one second, so max_cue_s is a word budget.
    assert [c.text for c in media.build_cues([("A", text)], wpm=60, max_cue_s=6)] == [
        "One two three. Four five six.",
        "Seven eight nine.",
    ]
    assert [c.text for c in media.build_cues([("A", text)], wpm=60, max_cue_s=4)] == [
        "One two three.",
        "Four five six.",
        "Seven eight nine.",
    ]
    assert len(media.build_cues([("A", text)], wpm=60, max_cue_s=60)) == 1


def test_build_cues_falls_back_to_commas_then_words() -> None:
    sentence = "Alpha beta gamma, delta epsilon zeta, eta theta iota."
    assert [c.text for c in media.build_cues([("A", sentence)], wpm=60, max_cue_s=6)] == [
        "Alpha beta gamma, delta epsilon zeta,",
        "eta theta iota.",
    ]
    ten = "a b c d e f g h i j"
    assert [c.text for c in media.build_cues([("A", ten)], wpm=60, max_cue_s=4)] == [
        "a b c d",
        "e f g h",
        "i j",
    ]


def test_build_cues_single_word_longer_than_max_is_clamped() -> None:
    (cue,) = media.build_cues([("A", "Supercalifragilistic")], wpm=60, max_cue_s=0.5)
    assert cue.duration_s == pytest.approx(0.5)


def test_build_cues_is_deterministic_and_round_trips_through_vtt() -> None:
    first = media.build_cues(TURNS, start_offset_s=1.234)
    assert first == media.build_cues(TURNS, start_offset_s=1.234)
    for tags in (True, False):
        for ids in ID_STYLES:
            assert media.parse_vtt(media.make_vtt(first, with_voice_tags=tags, cue_ids=ids)) == first
    assert media.make_vtt(first) == media.make_vtt(media.build_cues(TURNS, start_offset_s=1.234))


def test_build_cues_times_are_whole_milliseconds() -> None:
    for cue in media.build_cues(TURNS, wpm=137, gap_s=0.3333, start_offset_s=0.0007):
        assert round(cue.start_s * 1000) / 1000 == cue.start_s
        assert round(cue.end_s * 1000) / 1000 == cue.end_s


def test_build_cues_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="max_cue_s"):
        media.build_cues(TURNS, max_cue_s=0)
    with pytest.raises(ValueError, match="wpm"):
        media.build_cues(TURNS, wpm=0)
