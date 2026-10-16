"""Deterministic identifier factories.

Every provider gets its own :class:`IdFactory` seeded from the hub seed and the provider
name, so seeded objects have the same ids on every run (tests can hard-code them) while
ids minted at runtime stay unique and realistically formatted (GUIDs for Microsoft,
``cus_`` prefixes for Stripe, 32-hex SIDs for Twilio, 15/18-char ids for Salesforce...).
"""

from __future__ import annotations

import base64
import hashlib
import random
import string
import threading
import uuid

BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
ALNUM_LOWER = string.digits + string.ascii_lowercase
URLSAFE = string.ascii_letters + string.digits + "-_"


class IdFactory:
    def __init__(self, seed: str | int) -> None:
        self._seed = str(seed)
        self._rng = random.Random(self._seed)
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def fork(self, namespace: str) -> IdFactory:
        """An independent stream, so adding ids in one area never shifts another's."""
        return IdFactory(f"{self._seed}/{namespace}")

    def _choice(self, alphabet: str, n: int) -> str:
        with self._lock:
            return "".join(self._rng.choice(alphabet) for _ in range(n))

    def guid(self) -> str:
        with self._lock:
            return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))

    def hex(self, n: int = 32) -> str:
        return self._choice("0123456789abcdef", n)

    def digits(self, n: int, *, leading_nonzero: bool = True) -> str:
        value = self._choice(string.digits, n)
        if leading_nonzero and value[0] == "0":
            value = "1" + value[1:]
        return value

    def base62(self, n: int) -> str:
        return self._choice(BASE62, n)

    def alnum_lower(self, n: int) -> str:
        return self._choice(ALNUM_LOWER, n)

    def urlsafe(self, n: int) -> str:
        return self._choice(URLSAFE, n)

    def prefixed(self, prefix: str, n: int = 24, *, sep: str = "_") -> str:
        """Stripe-style ``cus_Qx3...`` ids."""
        return f"{prefix}{sep}{self.base62(n)}"

    def sequence(self, name: str, start: int = 1) -> int:
        """Monotonic integer sequences (QuickBooks ids, invoice numbers, Box ids...)."""
        with self._lock:
            value = self._counters.get(name, start - 1) + 1
            self._counters[name] = value
            return value

    def token(self, prefix: str = "", n: int = 40) -> str:
        return prefix + self.urlsafe(n)

    def randint(self, a: int, b: int) -> int:
        with self._lock:
            return self._rng.randint(a, b)

    def random(self) -> float:
        with self._lock:
            return self._rng.random()


def stable_hash(*parts: object, length: int = 16) -> str:
    """Short hex digest that depends only on ``parts`` — handy for ETags and stable ids."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return digest[:length]


def stable_guid(*parts: object) -> str:
    """A GUID derived from ``parts`` (same input, same GUID, across processes)."""
    return str(
        uuid.UUID(bytes=hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).digest()[:16], version=4)
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def salesforce_id(prefix: str, n: int) -> str:
    """18-character Salesforce id with the case-safe checksum suffix.

    ``prefix`` is the 3-char key prefix (``001`` Account, ``003`` Contact, ``006``
    Opportunity, ``00Q`` Lead, ``005`` User...). ``n`` is a sequence number.
    """
    body = f"{prefix}{'5g000'}{n:07d}"[:15]
    body = body.ljust(15, "0")
    suffix = ""
    alphabet = string.ascii_uppercase + "012345"
    for chunk in range(3):
        bits = 0
        for i, ch in enumerate(body[chunk * 5 : chunk * 5 + 5]):
            if ch.isupper():
                bits |= 1 << i
        suffix += alphabet[bits]
    return body + suffix
