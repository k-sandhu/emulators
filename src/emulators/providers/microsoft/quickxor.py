"""OneDrive's 160-bit rotating XOR hash, including its little-endian length suffix."""

from __future__ import annotations

import base64


def quickxor_hash(data: bytes) -> str:
    value = 0
    mask = (1 << 160) - 1
    for index, byte in enumerate(data):
        shifted = byte << ((index * 11) % 160)
        value ^= (shifted & mask) ^ (shifted >> 160)
    digest = bytearray(value.to_bytes(20, "little"))
    for index, byte in enumerate(len(data).to_bytes(8, "little")):
        digest[12 + index] ^= byte
    return base64.b64encode(digest).decode()
