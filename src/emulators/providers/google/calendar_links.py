"""Keep calendar invitations aligned with the other platforms' world projections."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import quote

from emulators.core.ids import stable_guid
from emulators.world.model import Meeting, World


def external_join_url(world: World, meeting: Meeting) -> str:
    if meeting.platform == "teams":
        token = base64.b64encode(stable_guid(world.seed, "ms-meeting-thread", meeting.id).encode()).decode()
        thread = quote(f"19:meeting_{token.rstrip('=')[:44]}@thread.v2", safe="")
        context = quote(
            json.dumps(
                {
                    "Tid": world.org.microsoft_tenant_id,
                    "Oid": stable_guid(world.seed, "ms-user", meeting.organizer_id),
                },
                separators=(",", ":"),
            ),
            safe="",
        )
        return f"https://teams.microsoft.com/l/meetup-join/{thread}/0?context={context}"

    def digest(*parts: object) -> bytes:
        return hashlib.sha256("\x1f".join(("zoom", world.seed, *(str(p) for p in parts))).encode()).digest()

    def number_for(*parts: object, digits: int) -> int:
        low = 10 ** (digits - 1)
        return low + int.from_bytes(digest(*parts)[:8], "big") % (9 * low)

    # Derive series IDs directly from the world; this also works in a Google-only hub.
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
