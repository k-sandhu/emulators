"""A short deterministic meeting script for meetings simulated at runtime.

Meetings seeded from the world already carry the world's transcripts; this only serves
``end_meeting`` for meetings created through the API (or world meetings run again)."""

from __future__ import annotations

import random

from emulators.world.media import estimate_speech_duration

OPENERS = (
    "Thanks everyone for joining. Today we'll cover {topic}, then agree next steps.",
    "Good to see everyone. The goal for this session is {topic}. Let's keep it focused.",
    "Welcome. We have a short agenda around {topic}, and I want to leave time for questions.",
)
STATUS_LINES = (
    "We've made good progress. Most of the open items are closed and two need input from the group.",
    "From my side the work is on track. The main risk is waiting on a few documents.",
    "I reviewed the latest numbers and they line up with what we expected last week.",
    "There are a couple of open questions I'd like to resolve before we move on.",
)
REPLY_LINES = (
    "Thanks. I'll send the updated figures by end of day tomorrow.",
    "That works for me. I can follow up with the team and confirm the dates.",
    "Understood. I'll take the action to update the tracker after this call.",
    "Agreed. Let's schedule a short check-in on Friday to review where we are.",
)
CLOSERS = (
    "To recap, we agreed the next steps and the owners. I'll send a follow-up note today. Thanks everyone.",
    "Great discussion. Action items are with the owners we named, and I'll circulate the summary. Talk soon.",
)


def script(topic: str, host: str, others: list[str], seed: str) -> list[tuple[str, str]]:
    """Alternate host/participant lines. ``others`` may be empty (host talking alone)."""
    rng = random.Random(seed)
    first = host.split()[0] if host else "everyone"
    lines: list[tuple[str, str]] = [(host, rng.choice(OPENERS).format(topic=topic or "the agenda"))]
    for i, other in enumerate(others or [host]):
        lines.append((other, rng.choice(STATUS_LINES)))
        lines.append((host, f"Thanks {other.split()[0]}. " + rng.choice(REPLY_LINES)))
        if i % 2 == 1:
            lines.append((other, f"{first}, one more question: who owns the follow-up on the open items?"))
            lines.append((host, "I'll own that and share the list after the meeting."))
    lines.append((host, rng.choice(CLOSERS)))
    return lines


def timed(lines: list[tuple[str, str]], span_s: float, seed: str) -> list[tuple[float, str, str]]:
    """Spread ``lines`` over ``span_s`` seconds: speech at ~155 wpm with pauses filling the slack."""
    rng = random.Random(seed + "/t")
    durations = [max(1.2, estimate_speech_duration(text, wpm=155)) for _, text in lines]
    slack = max(span_s - sum(durations) - 30.0, len(lines) * 0.5)
    weights = [rng.uniform(0.3, 1.7) for _ in lines]
    scale = slack / sum(weights)
    out: list[tuple[float, str, str]] = []
    t = rng.uniform(10.0, 25.0)
    for (speaker, text), dur, w in zip(lines, durations, weights, strict=True):
        out.append((round(t, 2), speaker, text))
        t += dur + w * scale
    return out
