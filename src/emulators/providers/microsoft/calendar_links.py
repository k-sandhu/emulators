"""Project external meeting links without importing another provider's store or code."""

from __future__ import annotations

import base64
import hashlib

from emulators.core.ids import stable_hash
from emulators.world.model import Meeting, World


def external_join_url(world: World, meeting: Meeting) -> str:
    if meeting.platform == "meet":
        number = int(stable_hash(world.seed, "meet-code", meeting.id, length=24), 16)
        letters = []
        for _ in range(10):
            number, remainder = divmod(number, 26)
            letters.append("abcdefghijklmnopqrstuvwxyz"[remainder])
        code = "".join(letters)
        return f"https://meet.google.com/{code[:3]}-{code[3:7]}-{code[7:]}"

    def digest(*parts: object) -> bytes:
        return hashlib.sha256("\x1f".join(("zoom", world.seed, *(str(p) for p in parts))).encode()).digest()

    def number_for(*parts: object, digits: int) -> int:
        low = 10 ** (digits - 1)
        return low + int.from_bytes(digest(*parts)[:8], "big") % (9 * low)

    # Recurring occurrences share the series' Zoom number and password. Resolve the
    # same rare number collisions in chronological order as the Zoom projection.
    used: set[int] = set()
    series: set[str] = set()
    target = meeting.series_id or meeting.id
    for source in sorted((m for m in world.meetings if m.platform == "zoom"), key=lambda m: (m.start, m.id)):
        key = source.series_id or source.id
        if key in series:
            continue
        series.add(key)
        digits = 11 if number_for("mlen", key, digits=2) % 2 else 10
        number = number_for("meeting", key, digits=digits)
        while number in used:
            number += 1
        used.add(number)
        if key == target:
            password = base64.b64encode(base64.b64encode(digest("pwd", number)[:18])).decode()
            return f"https://us06web.zoom.us/j/{number}?pwd={password}"
    raise ValueError(f"No Zoom meeting for {meeting.id}")
