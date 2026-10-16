"""Seed file metadata eagerly and render binary content only when a client needs it."""

from __future__ import annotations

from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.google.common import (
    drive_id,
    meet_artifact_name,
    meet_recording_file_id,
    meet_transcript_doc_id,
)
from emulators.world.documents import EXTENSION_TO_MIME

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDE = "application/vnd.google-apps.presentation"
NATIVE = {DOC, SHEET, SLIDE}
ROLES = {"owner": "owner", "editor": "writer", "commenter": "commenter", "viewer": "reader"}


def root_id(ctx: ProviderContext, person: str) -> str:
    return drive_id(ctx.world, "root", person)


def shared_drive_id(ctx: ProviderContext, space: str) -> str:
    return "0A" + drive_id(ctx.world, "shared-drive", space)[1:18]


def permission(
    ctx: ProviderContext, pid: str | None, role: str, *, email: str | None = None, link: str = "none"
) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": "drive#permission", "role": role, "deleted": False, "pendingOwner": False}
    if link == "anyone":
        return {**row, "id": "anyoneWithLink", "type": "anyone", "allowFileDiscovery": False}
    if link == "organization":
        return {
            **row,
            "id": ctx.world.org.domain,
            "type": "domain",
            "domain": ctx.world.org.domain,
            "allowFileDiscovery": False,
        }
    group = next((g for g in ctx.world.groups if g.id == pid), None)
    if group:
        email = group.email
        row.update(type="group", displayName=group.name)
    else:
        person = next((p for p in ctx.world.people if p.id == pid or (email and p.email == email)), None)
        if person:
            email = person.email
            row["displayName"] = person.display_name
        row["type"] = "user"
    row.update(id=str(int(stable_hash("perm", email or pid, length=16), 16))[:20], emailAddress=email)
    return row


def new_file(
    ctx: ProviderContext,
    fid: str,
    name: str,
    mime: str,
    owner: str | None,
    parents: list[str],
    *,
    drive: str | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    stamp = stamp or isoformat_z(ctx.world.anchor)
    row: dict[str, Any] = {
        "kind": "drive#file",
        "id": fid,
        "name": name,
        "mimeType": mime,
        "description": "",
        "starred": False,
        "trashed": False,
        "explicitlyTrashed": False,
        "parents": parents,
        "properties": {},
        "appProperties": {},
        "spaces": ["drive"],
        "version": "1",
        "createdTime": stamp,
        "modifiedTime": stamp,
        "viewedByMe": False,
        "shared": False,
        "viewersCanCopyContent": True,
        "copyRequiresWriterPermission": False,
        "writersCanShare": True,
        "hasThumbnail": mime not in (FOLDER, "text/plain", "text/csv", "text/markdown"),
        "thumbnailVersion": "1",
        "isAppAuthorized": True,
        "contentRestrictions": [],
        "linkShareMetadata": {"securityUpdateEligible": False, "securityUpdateEnabled": False},
        "labelInfo": {"labels": []},
        "contentHints": {},
        "_owner": owner,
        "_permissions": [],
        "_paragraphs": [],
        "_kind": "txt",
    }
    if drive:
        row.update(driveId=drive, teamDriveId=drive)
    elif owner:
        row["_permissions"] = [permission(ctx, owner, "owner")]
    return row


def seed(ctx: ProviderContext) -> None:
    files = ctx.store.collection("drive_files")
    for person in ctx.world.active_employees:
        files.put(new_file(ctx, root_id(ctx, person.id), "My Drive", FOLDER, person.id, []))
    for space in ctx.world.spaces:
        if space.kind != "team":
            continue
        did = shared_drive_id(ctx, space.id)
        drive = {
            "kind": "drive#drive",
            "id": did,
            "name": space.name,
            "colorRgb": "#4986e7",
            "createdTime": isoformat_z(ctx.world.anchor),
            "hidden": False,
            "restrictions": {
                "adminManagedRestrictions": False,
                "copyRequiresWriterPermission": False,
                "domainUsersOnly": False,
                "driveMembersOnly": False,
            },
            "_space": space.id,
        }
        ctx.store.collection("drive_drives").put(drive)
        root = new_file(ctx, did, space.name, FOLDER, space.owner_id, [], drive=did)
        root["_permissions"] = [permission(ctx, space.owner_id, "organizer")]
        root["_permissions"].extend(
            permission(ctx, pid, "organizer" if role == "owner" else ROLES[role])
            for pid, role in space.members
        )
        files.put(root)
    for space in ctx.world.spaces:
        drive = shared_drive_id(ctx, space.id) if space.kind == "team" else None
        base = drive or root_id(ctx, space.owner_id)
        for folder in ctx.world.folders_in(space.id):
            parent_path = folder.rsplit("/", 1)[0] if "/" in folder else ""
            parent = drive_id(ctx.world, "folder", space.id, parent_path) if parent_path else base
            fid = drive_id(ctx.world, "folder", space.id, folder)
            row = new_file(ctx, fid, folder.rsplit("/", 1)[-1], FOLDER, space.owner_id, [parent], drive=drive)
            if not drive:
                row["_permissions"] = []
            files.put(row)
    for doc in ctx.world.documents:
        space = ctx.world.space(doc.space_id)
        drive = shared_drive_id(ctx, space.id) if space.kind == "team" else None
        parent = (
            drive_id(ctx.world, "folder", space.id, doc.folder)
            if doc.folder
            else drive or root_id(ctx, space.owner_id)
        )
        native = space.id in ("knowledge", "people", "leadership", "growth") or space.kind == "personal"
        mime = (
            {"docx": DOC, "xlsx": SHEET, "pptx": SLIDE}.get(doc.kind, EXTENSION_TO_MIME[doc.kind])
            if native
            else EXTENSION_TO_MIME[doc.kind]
        )
        name = doc.name.rsplit(".", 1)[0] if mime in NATIVE else doc.name
        row = new_file(
            ctx,
            drive_id(ctx.world, "file", doc.id),
            name,
            mime,
            doc.owner_id,
            [parent],
            drive=drive,
            stamp=isoformat_z(doc.created),
        )
        row.update(
            description=doc.description,
            starred=doc.starred,
            trashed=doc.trashed,
            explicitlyTrashed=doc.trashed,
            modifiedTime=isoformat_z(doc.modified),
            version=str(doc.versions),
        )
        row.update(
            _world=doc.id,
            _kind=doc.kind,
            _paragraphs=list(doc.paragraphs),
            _table=[list(r) for r in doc.table] if doc.table else None,
            _title=doc.title,
            _restricted=doc.restricted,
            _modified_by=doc.modified_by_id,
        )
        if doc.kind == "png":
            row["imageMediaMetadata"] = {"width": 64, "height": 64, "rotation": 0}
        if doc.restricted:
            row["_permissions"] = [permission(ctx, doc.owner_id, "writer" if drive else "owner")]
        for share in doc.shares:
            perm = permission(ctx, share.principal_id, ROLES[share.role], email=share.email, link=share.link)
            if share.expires:
                perm["expirationTime"] = isoformat_z(share.expires)
            row["_permissions"].append(perm)
        files.put(row)
    for meeting in ctx.world.meetings:
        if meeting.platform != "meet" or not (meeting.recorded or meeting.transcribed):
            continue
        folder = drive_id(ctx.world, "meet-recordings-folder", meeting.organizer_id)
        if folder not in files:
            files.put(
                new_file(
                    ctx,
                    folder,
                    "Meet Recordings",
                    FOLDER,
                    meeting.organizer_id,
                    [root_id(ctx, meeting.organizer_id)],
                )
            )
        for what, enabled, fid, mime in (
            ("Recording", meeting.recorded, meet_recording_file_id(ctx.world, meeting.id), "video/mp4"),
            ("Transcript", meeting.transcribed, meet_transcript_doc_id(ctx.world, meeting.id), DOC),
        ):
            if not enabled:
                continue
            row = new_file(
                ctx,
                fid,
                meet_artifact_name(meeting, what),
                mime,
                meeting.organizer_id,
                [folder],
                stamp=isoformat_z(meeting.end),
            )
            row["_permissions"].extend(
                permission(ctx, pid, "reader")
                for pid in meeting.all_attendee_ids()
                if pid != meeting.organizer_id
            )
            if what == "Recording":
                row.update(
                    _kind="mp4",
                    _duration=meeting.duration_minutes * 60,
                    videoMediaMetadata={
                        "width": 1280,
                        "height": 720,
                        "durationMillis": str(meeting.duration_minutes * 60000),
                    },
                )
            else:
                row["_paragraphs"] = [
                    f"{ctx.world.person(turn.speaker_id).display_name}: {turn.text}"
                    for turn in meeting.transcript
                ]
            files.put(row)
    ctx.store.meta["drive_change_sequence"] = 0
