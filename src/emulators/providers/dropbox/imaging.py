"""Thumbnail encoders (stdlib only): PNG via the shared world generator, and a tiny baseline JPEG
encoder that stores one DC coefficient per 8x8 block (a valid, decodable, blocky image).

Dropbox thumbnails are real downscaled renderings; the emulator cannot decode arbitrary source
images, so it serves a deterministic gradient derived from the file id at the requested size --
what matters to callers is a decodable image of the right dimensions and content type.
"""

from __future__ import annotations

import hashlib
import struct

from emulators.core.errors import EmulatorError
from emulators.providers.dropbox.common import api_error, tag
from emulators.world import documents

# Standard JPEG luminance DC Huffman table (ITU T.81 Annex K): category -> (code, length).
_DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
_DC_VALS = list(range(12))
_DC_CODES = {
    0: (0b00, 2),
    1: (0b010, 3),
    2: (0b011, 3),
    3: (0b100, 3),
    4: (0b101, 3),
    5: (0b110, 3),
    6: (0b1110, 4),
    7: (0b11110, 5),
    8: (0b111110, 6),
    9: (0b1111110, 7),
    10: (0b11111110, 8),
    11: (0b111111110, 9),
}
_Q = 8  # DC quantiser: quantised DC == mean level (0..255) - 128


def _seed_for(file_id: str) -> int:
    return int.from_bytes(hashlib.sha256(file_id.encode()).digest()[:4], "big")


def _colors(seed: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    digest = hashlib.sha256(f"emulators-png:{seed}".encode()).digest()
    return tuple(digest[0:3]), tuple(digest[3:6])


def _segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


class _Bits:
    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, code: int, length: int) -> None:
        self.acc = (self.acc << length) | (code & ((1 << length) - 1))
        self.n += length
        while self.n >= 8:
            byte = (self.acc >> (self.n - 8)) & 0xFF
            self.out.append(byte)
            if byte == 0xFF:
                self.out.append(0)
            self.n -= 8
        self.acc &= (1 << self.n) - 1

    def flush(self) -> bytes:
        if self.n:
            self.put((1 << (8 - self.n)) - 1, 8 - self.n)
        return bytes(self.out)


def _rgb_to_ycc(r: float, g: float, b: float) -> tuple[float, float, float]:
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b + 128
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b + 128
    return y, cb, cr


def make_jpeg(width: int, height: int, seed: int) -> bytes:
    start, end = _colors(seed)
    span = max(width + height - 2, 1)
    cell = max(4, min(width, height) // 8)
    bw, bh = (width + 7) // 8, (height + 7) // 8
    bits = _Bits()
    prev = [0, 0, 0]
    for by in range(bh):
        for bx in range(bw):
            x = min(bx * 8 + 4, width - 1)
            y = min(by * 8 + 4, height - 1)
            t = x + y
            pixel = [a + (b - a) * t // span for a, b in zip(start, end, strict=True)]
            if (x // cell + y // cell) & 1:
                pixel = [max(v - 32, 0) for v in pixel]
            ycc = _rgb_to_ycc(*pixel)
            for comp in range(3):
                level = round(ycc[comp]) - 128
                level = max(-128, min(127, level))
                diff = level - prev[comp]
                prev[comp] = level
                size = abs(diff).bit_length()
                code, length = _DC_CODES[size]
                bits.put(code, length)
                if size:
                    bits.put(diff if diff > 0 else diff + (1 << size) - 1, size)
                bits.put(0, 1)  # AC table: the single code "0" is EOB
    scan = bits.flush()
    out = bytearray(b"\xff\xd8")
    out += _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    out += _segment(0xDB, bytes([0]) + bytes([_Q]) * 64)
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"".join(bytes([i, 0x11, 0]) for i in (1, 2, 3))
    out += _segment(0xC0, sof)
    out += _segment(0xC4, bytes([0x00]) + bytes(_DC_BITS) + bytes(_DC_VALS))
    out += _segment(0xC4, bytes([0x10]) + bytes([1] + [0] * 15) + bytes([0x00]))
    sos = bytes([3]) + b"".join(bytes([i, 0x00]) for i in (1, 2, 3)) + bytes([0, 63, 0])
    out += _segment(0xDA, sos)
    out += scan
    out += b"\xff\xd9"
    return bytes(out)


def encode_thumbnail(file_id: str, size: tuple[int, int], fmt: str) -> tuple[bytes, str]:
    width, height = size
    seed = _seed_for(file_id)
    if fmt == "png":
        return documents.make_png(width, height, seed=seed), "image/png"
    if fmt == "jpeg":
        return make_jpeg(width, height, seed), "image/jpeg"
    raise api_error(tag("unsupported_output_format"))


__all__ = ["EmulatorError", "encode_thumbnail", "make_jpeg"]
