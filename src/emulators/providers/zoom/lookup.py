"""Store lookups and the permission rules shared by the route modules."""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.zoom.common import (
    E_NOT_HOST,
    Principal,
    decode_path_key,
    is_meeting_id,
    meeting_missing,
    no_permission,
    owner_person,
    recording_missing,
    user_missing,
    zoom_error,
    zoom_user_id,
)

Record = dict[str, Any]


def users(ctx: ProviderContext) -> list[Record]:
    return ctx.store.collection("users").values()


def find_user(ctx: ProviderContext, key: str) -> Record | None:
    """A Zoom user by 22-char id, email address or world person id."""
    needle = key.strip().lower()
    coll = ctx.store.collection("users")
    direct = coll.get(key.strip())
    if direct is not None:
        return direct
    for rec in coll:
        if needle in (rec["email"].lower(), str(rec.get("_person_id") or "").lower()):
            return rec
    return None


def user_by_person(ctx: ProviderContext, person_id: str) -> Record | None:
    return ctx.store.collection("users").get(zoom_user_id(ctx.world, person_id))


def owner_user(ctx: ProviderContext) -> Record:
    rec = user_by_person(ctx, owner_person(ctx.world).id)
    assert rec is not None
    return rec


def acting_user(ctx: ProviderContext, principal: Principal, key: str, *, admin_level: bool) -> Record:
    """Resolve the ``{userId}`` path parameter. ``me`` is the token's user (the account owner for
    account-level apps); anyone else needs account-wide reach."""
    if key == "me":
        if principal.person is not None:
            rec = user_by_person(ctx, principal.person.id)
            if rec is not None:
                return rec
        return owner_user(ctx)
    rec = find_user(ctx, key)
    if rec is None:
        raise user_missing(key)
    if not admin_level and (principal.person is None or rec.get("_person_id") != principal.person.id):
        raise no_permission()
    return rec


def my_user_id(ctx: ProviderContext, principal: Principal) -> str | None:
    return zoom_user_id(ctx.world, principal.person.id) if principal.person else None


# -- meetings -----------------------------------------------------------------------------


def alt_hosts(meeting: Record) -> set[str]:
    raw = (meeting.get("settings") or {}).get("alternative_hosts") or ""
    return {h.strip().lower() for h in raw.replace(";", ",").split(",") if h.strip()}


def find_meeting(ctx: ProviderContext, meeting_id: str) -> Record:
    key = str(meeting_id).replace(" ", "")
    meeting = ctx.store.collection("meetings").get(key) if key.isdigit() else None
    if meeting is None:
        raise meeting_missing(meeting_id)
    return meeting


def meeting_by_uuid(ctx: ProviderContext, uuid: str) -> Record | None:
    for meeting in ctx.store.collection("meetings"):
        if meeting["uuid"] == uuid or any(o.get("_uuid") == uuid for o in meeting.get("occurrences", [])):
            return meeting
    inst = ctx.store.collection("instances").get(uuid)
    if inst is not None:
        return ctx.store.collection("meetings").get(str(inst["meeting_id"]))
    return None


def can_see_meeting(
    ctx: ProviderContext, principal: Principal, admin_level: bool, meeting: Record, *, write: bool
) -> bool:
    if admin_level:
        return True
    person = principal.person
    if person is None:
        return False
    if meeting["host_id"] == zoom_user_id(ctx.world, person.id):
        return True
    return not write and person.email.lower() in alt_hosts(meeting)


def ensure_meeting_access(
    ctx: ProviderContext, principal: Principal, admin_level: bool, meeting: Record, *, write: bool = False
) -> None:
    if not can_see_meeting(ctx, principal, admin_level, meeting, write=write):
        raise zoom_error(403, E_NOT_HOST, "You are not the meeting host.")


# -- instances and recordings -------------------------------------------------------------


def instances_of(ctx: ProviderContext, meeting_id: int) -> list[Record]:
    found = [i for i in ctx.store.collection("instances") if i["meeting_id"] == meeting_id]
    return sorted(found, key=lambda i: i["start_time"])


def find_instance(ctx: ProviderContext, raw_key: str, *, ended_only: bool = False) -> Record:
    """A meeting instance by UUID (single or double URL-encoded) or, as Zoom allows, by meeting id
    (the most recent instance; ``ended_only`` skips one that is still running)."""
    key = decode_path_key(raw_key)
    inst = ctx.store.collection("instances").get(key)
    if inst is not None:
        return inst
    if is_meeting_id(key):
        found = instances_of(ctx, int(key.replace(" ", "")))
        if ended_only:
            found = [i for i in found if i.get("state") == "ended"]
        if found:
            return found[-1]
    raise meeting_missing(key)


def find_recording(ctx: ProviderContext, raw_key: str, *, include_trashed: bool = False) -> Record:
    key = decode_path_key(raw_key)
    recs = ctx.store.collection("recordings")
    rec = recs.get(key)
    if rec is None and is_meeting_id(key):
        mine = [r for r in recs if str(r["meeting_id"]) == key.replace(" ", "")]
        mine.sort(key=lambda r: r["start_time"])
        rec = next((r for r in reversed(mine) if include_trashed or not r["trashed"]), None)
    if rec is None or (rec["trashed"] and not include_trashed):
        raise recording_missing()
    return rec


def can_see_recording(ctx: ProviderContext, principal: Principal, admin_level: bool, rec: Record) -> bool:
    if admin_level:
        return True
    person = principal.person
    return person is not None and rec["host_id"] == zoom_user_id(ctx.world, person.id)


def ensure_recording_access(
    ctx: ProviderContext, principal: Principal, admin_level: bool, rec: Record
) -> None:
    if not can_see_recording(ctx, principal, admin_level, rec):
        raise no_permission()
