"""Tree mutations shared by routes and simulation actions: create, upload, copy, move, trash, purge."""

from __future__ import annotations

import copy
from typing import Any

from emulators.core.clock import utc
from emulators.core.provider import ProviderContext
from emulators.providers.box import content, db, errors, metadata, tree
from emulators.providers.box.access import Principal
from emulators.providers.box.shapes import Shaper
from emulators.providers.box.util import next_id, now_iso, sha1_hex, try_parse_ts

Record = dict[str, Any]

MAX_SIMPLE_UPLOAD = 50 * 1024 * 1024
VERSION_ID_START = 9_000_000_000_000


def mini_conflict(p: Principal, record: Record) -> Record:
    shaper = Shaper(p)
    return shaper.file_mini(record) if record["type"] == "file" else shaper.folder_mini(record)


def ensure_name_free(
    p: Principal, parent_id: str, name: str, *, ignore: str | None = None, as_object: bool = False
) -> None:
    clash = p.tree.find_by_name(parent_id, name, ignore=ignore)
    if clash is not None:
        conflict = mini_conflict(p, clash)
        raise errors.name_in_use(conflict if as_object else [conflict])


def touch_parent(ctx: ProviderContext, parent: Record | None, p: Principal, now: str) -> None:
    if parent is not None and not db.is_root(parent):
        parent["modified_at"] = now
        parent["content_modified_at"] = now


def touch(record: Record, p: Principal, now: str, *, bump: bool = True) -> None:
    record["modified_at"] = now
    record["modified_by"] = p.uid
    if bump:
        record["_seq"] = int(record.get("_seq", 0)) + 1


def check_if_match(request_headers: Any, record: Record) -> None:
    expected = request_headers.get("if-match")
    if expected is not None and expected.strip('"') != str(record["_seq"]):
        raise errors.precondition_failed()


def lock_active(ctx: ProviderContext, record: Record) -> Record | None:
    lock = record.get("lock")
    if not lock:
        return None
    expires = try_parse_ts(lock.get("expired_at"))
    if expires and utc(expires) <= ctx.clock.now():
        record["lock"] = None
        return None
    return lock


def ensure_unlocked(ctx: ProviderContext, record: Record, p: Principal) -> None:
    lock = lock_active(ctx, record)
    if lock and lock["created_by"] != p.uid:
        raise errors.box_error(403, "access_denied_item_locked", "Access denied - item locked")


def record_recent(ctx: ProviderContext, user_id: str, record: Record, interaction: str) -> None:
    recents = ctx.store.collection(db.RECENTS)
    stale = [
        r["id"]
        for r in recents
        if r["user_id"] == user_id and r["item_id"] == record["id"] and r["item_type"] == record["type"]
    ]
    for rid in stale:
        recents.delete(rid)
    rid = next_id(ctx, "recent", recents, start=1_000_000)
    recents.put_as(
        rid,
        {
            "id": rid,
            "user_id": user_id,
            "item_type": record["type"],
            "item_id": record["id"],
            "interaction_type": interaction,
            "interacted_at": now_iso(ctx),
        },
    )


# -- creation ------------------------------------------------------------------------
def create_folder(p: Principal, parent: Record, name: str, now: str) -> Record:
    ctx = p.ctx
    ensure_name_free(p, parent["id"], name)
    fid = next_id(ctx, "folder", db.folders(ctx), start=db.FOLDER_ID_START)
    owner = parent.get("_owner") or p.uid
    record: Record = {
        "type": "folder",
        "id": fid,
        "name": name,
        "description": "",
        "_parent": parent["id"],
        "_owner": owner,
        "created_by": p.uid,
        "modified_by": p.uid,
        "created_at": now,
        "modified_at": now,
        "content_created_at": now,
        "content_modified_at": now,
        "trashed_at": None,
        "purged_at": None,
        "_trash": False,
        "_seq": 0,
        "shared_link": None,
        "tags": [],
        "folder_upload_email": None,
        "sync_state": "not_synced",
    }
    db.folders(ctx).put(record)
    touch_parent(ctx, parent, p, now)
    p.invalidate()
    return record


def new_version_record(
    ctx: ProviderContext, record: Record, data: bytes, p: Principal, now: str, number: int
) -> Record:
    vid = next_id(ctx, "version", db.versions(ctx), start=VERSION_ID_START)
    blob = ctx.store.blobs.put(data)
    version: Record = {
        "type": "file_version",
        "id": vid,
        "_file": record["id"],
        "number": number,
        "name": record["name"],
        "sha1": sha1_hex(data),
        "size": len(data),
        "_blob": blob,
        "created_at": now,
        "modified_at": now,
        "modified_by": p.uid,
        "trashed_at": None,
        "trashed_by": None,
        "restored_at": None,
        "restored_by": None,
        "purged_at": None,
    }
    db.versions(ctx).put(version)
    return version


def create_file(
    p: Principal,
    parent: Record,
    name: str,
    data: bytes,
    now: str,
    *,
    content_created_at: str | None = None,
    content_modified_at: str | None = None,
    description: str = "",
) -> Record:
    ctx = p.ctx
    ensure_name_free(p, parent["id"], name, as_object=True)
    fid = next_id(ctx, "file", db.files(ctx), start=db.FILE_ID_START)
    owner = parent.get("_owner") or p.uid
    record: Record = {
        "type": "file",
        "id": fid,
        "name": name,
        "description": description,
        "_parent": parent["id"],
        "_owner": owner,
        "created_by": p.uid,
        "modified_by": p.uid,
        "created_at": now,
        "modified_at": now,
        "content_created_at": content_created_at or now,
        "content_modified_at": content_modified_at or now,
        "trashed_at": None,
        "purged_at": None,
        "_trash": False,
        "_seq": 0,
        "shared_link": None,
        "tags": [],
        "lock": None,
        "expires_at": None,
        "sha1": sha1_hex(data),
        "size": len(data),
        "_blob": ctx.store.blobs.put(data),
        "_doc": None,
        "_text": text_of(data),
    }
    version = new_version_record(ctx, record, data, p, now, 1)
    record["_version"] = version["id"]
    db.files(ctx).put(record)
    touch_parent(ctx, parent, p, now)
    record_recent(ctx, p.uid, record, "item_upload")
    p.invalidate()
    return record


def text_of(data: bytes) -> str:
    """Searchable text of plain-text uploads (binary formats are searchable by name only)."""
    try:
        return data[:200_000].decode("utf-8").lower()
    except UnicodeDecodeError:
        return ""


def add_version(
    p: Principal,
    record: Record,
    data: bytes,
    now: str,
    *,
    name: str | None = None,
    content_modified_at: str | None = None,
) -> Record:
    ctx = p.ctx
    current = db.versions(ctx).get(record["_version"]) or {"number": 0}
    if name and name != record["name"]:
        ensure_name_free(p, record["_parent"], name, ignore=record["id"], as_object=True)
        record["name"] = name
    version = new_version_record(ctx, record, data, p, now, int(current["number"]) + 1)
    version["name"] = record["name"]
    record.update(
        _version=version["id"], sha1=version["sha1"], size=version["size"], _blob=version["_blob"],
        content_modified_at=content_modified_at or now, _text=text_of(data),
    )  # fmt: skip
    touch(record, p, now)
    record_recent(ctx, p.uid, record, "item_modify")
    p.invalidate()
    return record


# -- copy ------------------------------------------------------------------------------
def copy_file(
    p: Principal, source: Record, parent: Record, name: str | None, now: str, version_id: str | None = None
) -> Record:
    ctx = p.ctx
    target_name = name or source["name"]
    ensure_name_free(p, parent["id"], target_name, as_object=True)
    fid = next_id(ctx, "file", db.files(ctx), start=db.FILE_ID_START)
    data = content.file_bytes(ctx, source, version_id)
    record = copy.deepcopy(source)
    record.update(
        id=fid, name=target_name, _parent=parent["id"], _owner=parent.get("_owner") or p.uid,
        created_by=p.uid, modified_by=p.uid, created_at=now, modified_at=now, _seq=0, shared_link=None,
        lock=None, _trash=False, trashed_at=None, sha1=sha1_hex(data), size=len(data),
        _blob=ctx.store.blobs.put(data),
    )  # fmt: skip
    version = new_version_record(ctx, record, data, p, now, 1)
    record["_version"] = version["id"]
    db.files(ctx).put(record)
    metadata.copy_instances(ctx, source, record, now)
    touch_parent(ctx, parent, p, now)
    p.invalidate()
    return record


def copy_folder(p: Principal, source: Record, parent: Record, name: str | None, now: str) -> Record:
    ctx = p.ctx
    if parent["id"] == source["id"] or tree.is_within(ctx, parent, source["id"]):
        raise errors.bad_request(
            "Folder cannot be copied into itself or one of its descendants",
            reason="invalid_parameter",
            name="parent",
        )
    target = create_folder(p, parent, name or source["name"], now)
    target["description"] = source.get("description", "")
    target["tags"] = list(source.get("tags", []))
    metadata.copy_instances(ctx, source, target, now)
    for child in p.tree.children(source["id"]):
        if not p.visible(child):
            continue
        if child["type"] == "folder":
            copy_folder(p, child, target, None, now)
        elif child["type"] == "file":
            copy_file(p, child, target, None, now)
    p.invalidate()
    return target


# -- move / trash / restore / purge ----------------------------------------------------
def reown(p: Principal, record: Record, owner: str) -> None:
    record["_owner"] = owner
    if record["type"] == "folder":
        for child in p.tree.all_children(record["id"]):
            reown(p, child, owner)


def ensure_not_frozen(ctx: ProviderContext, record: Record, operation: str) -> None:
    """Folder locks ("freeze") forbid moving or deleting the locked folder."""
    if record["type"] != "folder":
        return
    for lock in ctx.store.collection(db.FOLDER_LOCKS):
        if lock["folder_id"] == record["id"] and lock["locked_operations"].get(operation):
            raise errors.box_error(403, "access_denied_item_locked", "Access denied - item locked")


def move(p: Principal, record: Record, new_parent: Record, now: str) -> None:
    ctx = p.ctx
    ensure_not_frozen(ctx, record, "move")
    if record["type"] == "folder" and (
        new_parent["id"] == record["id"] or tree.is_within(ctx, new_parent, record["id"])
    ):
        raise errors.bad_request(
            "Folder cannot be moved into itself or one of its descendants",
            reason="invalid_parameter",
            name="parent",
        )
    ensure_name_free(p, new_parent["id"], record["name"], ignore=record["id"])
    old_parent = tree.parent_of(ctx, record)
    record["_parent"] = new_parent["id"]
    new_owner = new_parent.get("_owner") or new_parent.get("_root_of")
    if new_owner and new_owner != record.get("_owner"):
        reown(p, record, new_owner)
    touch_parent(ctx, old_parent, p, now)
    touch_parent(ctx, new_parent, p, now)
    p.invalidate()


def trash(p: Principal, record: Record, now: str) -> None:
    ensure_not_frozen(p.ctx, record, "delete")
    record["_trash"] = True
    record["trashed_at"] = now
    record["_trashed_by"] = p.uid
    touch_parent(p.ctx, tree.parent_of(p.ctx, record), p, now)
    p.invalidate()


def restore(
    p: Principal, record: Record, now: str, *, parent: Record | None = None, name: str | None = None
) -> None:
    ctx = p.ctx
    target_parent = parent or tree.parent_of(ctx, record)
    if target_parent is None or tree.is_trashed(ctx, target_parent):
        raise errors.forbidden("Cannot restore item: its original parent folder is no longer available")
    target_name = name or record["name"]
    ensure_name_free(p, target_parent["id"], target_name, ignore=record["id"])
    record["name"] = target_name
    record["_parent"] = target_parent["id"]
    record["_trash"] = False
    record["trashed_at"] = None
    record.pop("_trashed_by", None)
    new_owner = target_parent.get("_owner") or target_parent.get("_root_of")
    if new_owner and new_owner != record.get("_owner"):
        reown(p, record, new_owner)
    record["_seq"] = int(record.get("_seq", 0)) + 1
    p.invalidate()


def drop_item(ctx: ProviderContext, record: Record) -> None:
    """Permanently remove an item and everything hanging off it."""
    if record["type"] == "folder":
        kids = [
            r
            for coll in (db.folders(ctx), db.files(ctx), ctx.store.collection(db.WEB_LINKS))
            for r in coll
            if r.get("_parent") == record["id"]
        ]
        for child in kids:
            drop_item(ctx, child)
        db.folders(ctx).delete(record["id"])
    elif record["type"] == "file":
        for version in [v for v in db.versions(ctx) if v["_file"] == record["id"]]:
            db.versions(ctx).delete(version["id"])
        for comment in [
            c
            for c in ctx.store.collection(db.COMMENTS)
            if c["item_type"] == "file" and c["item_id"] == record["id"]
        ]:
            ctx.store.collection(db.COMMENTS).delete(comment["id"])
        for task in [t for t in ctx.store.collection(db.TASKS) if t["item_id"] == record["id"]]:
            for assignment in [a for a in ctx.store.collection(db.ASSIGNMENTS) if a["task_id"] == task["id"]]:
                ctx.store.collection(db.ASSIGNMENTS).delete(assignment["id"])
            ctx.store.collection(db.TASKS).delete(task["id"])
        db.files(ctx).delete(record["id"])
    else:
        ctx.store.collection(db.WEB_LINKS).delete(record["id"])
    for collab in [
        c for c in db.collabs(ctx) if c["item_type"] == record["type"] and c["item_id"] == record["id"]
    ]:
        db.collabs(ctx).delete(collab["id"])
    for inst in metadata.instances_for(ctx, record):
        metadata.delete_instance(ctx, inst)
    for webhook in [
        w
        for w in ctx.store.collection(db.WEBHOOKS)
        if w["target"]["id"] == record["id"] and w["target"]["type"] == record["type"]
    ]:
        ctx.store.collection(db.WEBHOOKS).delete(webhook["id"])
    for lock in [x for x in ctx.store.collection(db.FOLDER_LOCKS) if x["folder_id"] == record["id"]]:
        ctx.store.collection(db.FOLDER_LOCKS).delete(lock["id"])
    for coll in ctx.store.collection(db.COLLECTIONS):
        items = coll["items"].get(record["type"], [])
        if record["id"] in items:
            items.remove(record["id"])


def purge(p: Principal, record: Record) -> None:
    drop_item(p.ctx, record)
    p.invalidate()


# -- favourites -----------------------------------------------------------------------
def user_collections(ctx: ProviderContext, user_id: str) -> list[Record]:
    return [c for c in ctx.store.collection(db.COLLECTIONS) if c["user_id"] == user_id]


def set_collections(p: Principal, record: Record, value: Any) -> None:
    """``collections: [{"id": ...}]`` on an item update: adds to listed, removes from unlisted."""
    if value is None:
        value = []
    if not isinstance(value, list):
        raise errors.invalid_parameter("collections", "'collections' must be an array")
    wanted = {str(c.get("id")) for c in value if isinstance(c, dict)}
    mine = user_collections(p.ctx, p.uid)
    unknown = wanted - {c["id"] for c in mine}
    if unknown:
        raise errors.not_found("Collection not found")
    for coll in mine:
        items = coll["items"].setdefault(record["type"], [])
        if coll["id"] in wanted and record["id"] not in items:
            items.append(record["id"])
        elif coll["id"] not in wanted and record["id"] in items:
            items.remove(record["id"])


# -- users created on the fly ---------------------------------------------------------
def create_external_user(ctx: ProviderContext, login: str, now: str, *, name: str | None = None) -> Record:
    """A Box account for an invited outsider (client contact, or an unregistered e-mail)."""
    uid = next_id(ctx, "user", db.users(ctx), start=db.USER_ID_START)
    display = name or login.split("@", 1)[0].replace(".", " ").title()
    record: Record = {
        "type": "user", "id": uid, "name": display, "login": login, "created_at": now, "modified_at": now,
        "language": "en", "timezone": "America/Toronto", "status": "active", "job_title": "", "phone": "",
        "address": "", "role": "user", "_pid": None, "_kind": "external", "_root": db.root_id(uid),
        "_invited": True,
    }  # fmt: skip
    db.users(ctx).put(record)
    db.folders(ctx).put(
        {
            "type": "folder", "id": record["_root"], "name": "All Files", "_root_of": uid, "_owner": uid,
            "_parent": None, "_seq": 0, "created_at": now, "modified_at": now,
        }
    )  # fmt: skip
    ctx.store.collection(db.COLLECTIONS).put(
        {
            "type": "collection",
            "id": next_id(ctx, "collection", ctx.store.collection(db.COLLECTIONS), start=db.SMALL_ID_START),
            "user_id": uid,
            "name": "Favorites",
            "collection_type": "favorites",
            "items": {"file": [], "folder": []},
        }
    )
    return record
