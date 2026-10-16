"""Shared Google API helpers: principals, both error envelope styles, stable ids.

Google has two error styles:

* "v1 discovery" APIs (Drive v3, Gmail v1, Calendar v3, Admin Directory v1) return
  ``{"error": {"code", "message", "errors": [{"message","domain","reason",...}], "status"}}``;
* newer gRPC-transcoded APIs (Meet v2) return ``{"error": {"code", "message", "status"}}``.

Delegated vs service-account (domain-wide delegation) access is modelled by
:class:`Principal`; tokens minted for a user act as that user, app-only tokens act as the
service account and may impersonate via the ``sub`` claim at token time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from emulators.core.auth import bearer_token
from emulators.core.errors import EmulatorError
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.world.model import Meeting, Person, World

STATUS_BY_CODE = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ALREADY_EXISTS",
    410: "GONE",
    412: "FAILED_PRECONDITION",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    503: "UNAVAILABLE",
}


# -- ids -------------------------------------------------------------------------------


def numeric_id(world: World, *parts: object, length: int = 21) -> str:
    """Google account/user ids are 21-digit decimal strings starting with 1."""
    digits = str(int(stable_hash(world.seed, "google", *parts, length=32), 16))
    return ("1" + digits)[:length]


def user_id(world: World, person_id: str) -> str:
    return numeric_id(world, "user", person_id)


def drive_id(world: World, *parts: object, length: int = 33) -> str:
    """Drive file ids: 33 chars of [A-Za-z0-9_-], usually starting with '1'."""
    import base64
    import hashlib

    raw = hashlib.sha256("\x1f".join(map(str, (world.seed, "gdrive", *parts))).encode()).digest()
    text = base64.urlsafe_b64encode(raw + raw).decode().rstrip("=")
    return ("1" + text)[:length]


def person_ref(person: Person) -> dict[str, Any]:
    """Drive ``user`` resource for owners / lastModifyingUser."""
    return {
        "kind": "drive#user",
        "displayName": person.display_name,
        "photoLink": f"https://lh3.googleusercontent.com/a/default-user=s64?{person.username}",
        "me": False,
        "permissionId": str(int(stable_hash("perm", person.email, length=16), 16))[:20],
        "emailAddress": person.email,
    }


# -- errors ----------------------------------------------------------------------------


def error_body(
    code: int,
    message: str,
    *,
    reason: str | None = None,
    domain: str = "global",
    location: str | None = None,
    legacy: bool = True,
    status: str | None = None,
) -> dict[str, Any]:
    status = status or STATUS_BY_CODE.get(code, "UNKNOWN")
    error: dict[str, Any] = {"code": code, "message": message}
    if legacy:
        item: dict[str, Any] = {"message": message, "domain": domain, "reason": reason or status.lower()}
        if location:
            item.update({"location": location, "locationType": "parameter"})
        error["errors"] = [item]
    error["status"] = status
    return {"error": error}


def google_error(code: int, message: str, **kwargs: Any) -> EmulatorError:
    return EmulatorError(code, error_body(code, message, **kwargs))


# -- principals ------------------------------------------------------------------------


@dataclass
class Principal:
    person_id: str | None  # acting world person (delegated, or impersonated by a service account)
    app_only: bool
    client_id: str
    scopes: list[str] = field(default_factory=list)


def principal(request: Request, ctx: ProviderContext, *, legacy: bool = True) -> Principal:
    token = bearer_token(request)
    if not token:
        token = request.query_params.get("access_token")
    grant = ctx.tokens.resolve(token)
    if grant is None:
        raise EmulatorError(
            401,
            error_body(
                401,
                "Request had invalid authentication credentials. Expected OAuth 2 access token, "
                "login cookie or other valid authentication credential.",
                reason="authError",
                location="Authorization",
                legacy=legacy,
            ),
            headers={"WWW-Authenticate": 'Bearer realm="https://accounts.google.com/"'},
        )
    person_id = grant.extra.get("sub_person") or grant.user_id
    if person_id and "@" in person_id:
        person = ctx.world.person_by_email(person_id)
        person_id = person.id if person else None
    return Principal(person_id, grant.app_only and not person_id, grant.client_id, list(grant.scopes))


# -- cross-module identifiers ------------------------------------------------------------
# Calendar (hangoutLink), Meet (spaces, conferenceRecords) and Drive (recordings and
# transcripts in the organizer's "Meet Recordings" folder) must agree on these.


def meet_code(world: World, meeting_id: str) -> str:
    """``abc-defg-hij`` meeting code."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    n = int(stable_hash(world.seed, "meet-code", meeting_id, length=24), 16)
    chars = []
    for _ in range(10):
        n, r = divmod(n, 26)
        chars.append(letters[r])
    code = "".join(chars)
    return f"{code[:3]}-{code[3:7]}-{code[7:]}"


def meet_uri(world: World, meeting_id: str) -> str:
    return f"https://meet.google.com/{meet_code(world, meeting_id)}"


def meet_space_name(world: World, meeting_id: str) -> str:
    return "spaces/" + drive_id(world, "meet-space", meeting_id, length=12)


def conference_record_name(world: World, meeting_id: str) -> str:
    return "conferenceRecords/" + drive_id(world, "meet-conf", meeting_id, length=36).replace("_", "-")


def meet_recording_file_id(world: World, meeting_id: str) -> str:
    """Drive file id of the MP4 recording in the organizer's ``Meet Recordings`` folder."""
    return drive_id(world, "meet-recording", meeting_id)


def meet_transcript_doc_id(world: World, meeting_id: str) -> str:
    """Drive id of the Google Doc transcript in the organizer's ``Meet Recordings`` folder."""
    return drive_id(world, "meet-transcript", meeting_id, length=44)


def meet_artifact_name(meeting: Meeting, what: str) -> str:
    """Drive file names Meet uses, e.g. ``Weekly sync (2026-10-14 10:02 GMT-4) - Recording``."""
    return f"{meeting.title} ({meeting.start:%Y-%m-%d %H:%M} GMT) - {what}"


async def json_object(request: Request, *, legacy: bool = True) -> dict[str, Any]:
    """Reject malformed JSON before resource code can accidentally produce a 500."""
    try:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value
    except ValueError as exc:
        raise google_error(400, "Invalid JSON payload received.", reason="badRequest", legacy=legacy) from exc
