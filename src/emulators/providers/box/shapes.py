"""Box wire shapes: users, groups, folders, files, collaborations and friends.

Records live in the store in a hybrid form (wire field names, ids instead of nested user
objects, ``_private`` bookkeeping); this module turns them into the exact JSON Box returns,
including the ``fields=`` rules: with no ``fields`` an endpoint returns its standard set,
with ``fields`` it returns the *mini* representation plus exactly what was asked for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request

from emulators.core.provider import ProviderContext, url_for_host
from emulators.providers.box import access, db, tree
from emulators.providers.box.access import Principal
from emulators.providers.box.util import box_ts, extension_of, try_parse_ts

Record = dict[str, Any]
Thunks = dict[str, Callable[[], Any]]

ALL_FILES = {"type": "folder", "id": "0", "sequence_id": None, "etag": None, "name": "All Files"}
TRASH = {"type": "folder", "id": "1", "sequence_id": None, "etag": None, "name": "Trash"}

USER_MINI = ("type", "id", "name", "login")
USER_ORDER = (
    "type", "id", "name", "login", "created_at", "modified_at", "language", "timezone", "space_amount",
    "space_used", "max_upload_size", "status", "job_title", "phone", "address", "avatar_url", "role",
    "tracking_codes", "can_see_managed_users", "is_sync_enabled", "is_external_collab_restricted",
    "is_exempt_from_device_limits", "is_exempt_from_login_verification", "enterprise", "my_tags",
    "hostname", "is_platform_access_only", "notification_email",
)  # fmt: skip
USER_STANDARD = USER_ORDER[:16]

FILE_ORDER = (
    "type", "id", "file_version", "sequence_id", "etag", "sha1", "name", "description", "size",
    "path_collection", "created_at", "modified_at", "trashed_at", "purged_at", "content_created_at",
    "content_modified_at", "created_by", "modified_by", "owned_by", "shared_link", "parent",
    "item_status", "extension", "version_number", "comment_count", "permissions", "tags", "lock",
    "has_collaborations", "is_externally_owned", "expires_at", "allowed_invitee_roles",
    "watermark_info", "is_accessible_via_shared_link", "collections", "metadata", "representations",
    "classification", "uploader_display_name", "disposition_at", "shared_link_permission_options",
    "is_package", "is_accessible_via_shared_link",
)  # fmt: skip
FILE_MINI = ("type", "id", "file_version", "sequence_id", "etag", "sha1", "name")
FILE_STANDARD = FILE_ORDER[:22]

FOLDER_ORDER = (
    "type", "id", "sequence_id", "etag", "name", "created_at", "modified_at", "description", "size",
    "path_collection", "created_by", "modified_by", "trashed_at", "purged_at", "content_created_at",
    "content_modified_at", "owned_by", "shared_link", "folder_upload_email", "parent", "item_status",
    "item_collection", "sync_state", "has_collaborations", "permissions", "tags",
    "can_non_owners_invite", "is_externally_owned", "is_collaboration_restricted_to_enterprise",
    "allowed_shared_link_access_levels", "allowed_invitee_roles", "watermark_info",
    "is_accessible_via_shared_link", "can_non_owners_view_collaborators", "classification",
    "collections", "metadata", "expires_at",
)  # fmt: skip
FOLDER_MINI = ("type", "id", "sequence_id", "etag", "name")
FOLDER_STANDARD = FOLDER_ORDER[:22]

WEB_LINK_ORDER = (
    "type", "id", "sequence_id", "etag", "name", "url", "created_at", "modified_at", "description",
    "path_collection", "created_by", "modified_by", "trashed_at", "purged_at", "shared_link", "parent",
    "item_status",
)  # fmt: skip
WEB_LINK_MINI = ("type", "id", "sequence_id", "etag", "name", "url")

FOLDER_ITEM_ORDER = [{"by": "type", "direction": "ASC"}, {"by": "name", "direction": "ASC"}]
ALL_INVITEE_ROLES = [
    "editor", "viewer", "previewer", "uploader", "previewer uploader", "viewer uploader", "co-owner",
]  # fmt: skip

DEFAULT_SPACE = 999_999_999_999_999
MAX_UPLOAD_MANAGED = 16_106_127_360
MAX_UPLOAD_EXTERNAL = 262_144_000


def pick(order: tuple[str, ...], requested: list[str] | None, standard: tuple[str, ...], mini: tuple[str, ...]) -> list[str]:
    """Field names to emit, in Box's canonical order."""
    if requested is None:
        return list(dict.fromkeys(standard))
    wanted = set(mini) | {f.split(".", 1)[0] for f in requested}
    return [n for n in dict.fromkeys(order) if n in wanted]


def build(names: list[str], thunks: Thunks) -> Record:
    return {n: thunks[n]() for n in names if n in thunks}


class Shaper:
    """Renders store records for one request (needs the principal and the request for URLs)."""

    def __init__(self, principal: Principal, request: Request | None = None) -> None:
        self.p = principal
        self.ctx: ProviderContext = principal.ctx
        self.request = request
        self._space_used: dict[str, int] | None = None

    # -- urls --------------------------------------------------------------------------
    def host_url(self, host: str, path: str) -> str:
        if self.request is None:
            return f"https://{host}{path}"
        return url_for_host(self.request, self.ctx, host, path)

    # -- users -------------------------------------------------------------------------
    def user_mini(self, user_id: str | None) -> Record | None:
        if not user_id:
            return None
        user = db.users(self.ctx).get(user_id)
        if user is None:
            return {"type": "user", "id": user_id, "name": "Deleted User", "login": ""}
        return {"type": "user", "id": user["id"], "name": user["name"], "login": user["login"]}

    def _space_used_for(self, user_id: str) -> int:
        if self._space_used is None:
            totals: dict[str, int] = {}
            for record in db.files(self.ctx):
                if not record.get("_purged"):
                    totals[record["_owner"]] = totals.get(record["_owner"], 0) + int(record["size"])
            self._space_used = totals
        return self._space_used.get(user_id, 0)

    def user(self, user: Record, fields: list[str] | None = None, *, full: bool = False) -> Record:
        external = user.get("_kind") == "external"
        enterprise = self.ctx.world.org
        thunks: Thunks = {
            "type": lambda: "user",
            "id": lambda: user["id"],
            "name": lambda: user["name"],
            "login": lambda: user["login"],
            "created_at": lambda: box_ts(user["created_at"]),
            "modified_at": lambda: box_ts(user["modified_at"]),
            "language": lambda: user.get("language", "en"),
            "timezone": lambda: user.get("timezone", "America/Toronto"),
            "space_amount": lambda: 10_737_418_240 if external else DEFAULT_SPACE,
            "space_used": lambda: self._space_used_for(user["id"]),
            "max_upload_size": lambda: MAX_UPLOAD_EXTERNAL if external else MAX_UPLOAD_MANAGED,
            "status": lambda: user["status"],
            "job_title": lambda: user.get("job_title", ""),
            "phone": lambda: user.get("phone", ""),
            "address": lambda: user.get("address", ""),
            "avatar_url": lambda: self.host_url("app.box.com", f"/api/avatar/large/{user['id']}"),
            "role": lambda: user.get("role", "user"),
            "tracking_codes": lambda: [],
            "can_see_managed_users": lambda: not external,
            "is_sync_enabled": lambda: not external,
            "is_external_collab_restricted": lambda: bool(user.get("is_external_collab_restricted", False)),
            "is_exempt_from_device_limits": lambda: False,
            "is_exempt_from_login_verification": lambda: False,
            "enterprise": lambda: None
            if external
            else {"type": "enterprise", "id": enterprise.box_enterprise_id, "name": enterprise.name},
            "my_tags": lambda: [],
            "hostname": lambda: "https://app.box.com/",
            "is_platform_access_only": lambda: user.get("_kind") == "service",
            "notification_email": lambda: user.get("notification_email"),
        }
        names = pick(USER_ORDER, fields, USER_ORDER if full else USER_STANDARD, USER_MINI)
        return build(names, thunks)

    # -- groups ------------------------------------------------------------------------
    def group_mini(self, group: Record) -> Record:
        return {"type": "group", "id": group["id"], "name": group["name"], "group_type": group["group_type"]}

    def group(self, group: Record, fields: list[str] | None = None) -> Record:
        full = {
            **self.group_mini(group),
            "created_at": box_ts(group["created_at"]),
            "modified_at": box_ts(group["modified_at"]),
        }
        extras = {
            "provenance": group.get("provenance"),
            "external_sync_identifier": group.get("external_sync_identifier"),
            "description": group.get("description", ""),
            "invitability_level": group.get("invitability_level", "admins_and_members"),
            "member_viewability_level": group.get("member_viewability_level", "admins_and_members"),
            "permissions": {"can_invite_as_collaborator": self.p.is_privileged},
        }
        if fields is None:
            return {**full, **extras} if self.p.is_privileged else full
        wanted = {f.split(".", 1)[0] for f in fields}
        return {**full, **{k: v for k, v in extras.items() if k in wanted}}

    # -- item parts --------------------------------------------------------------------
    def folder_mini(self, folder: Record) -> Record:
        if db.is_root(folder):
            return dict(ALL_FILES)
        seq = str(folder["_seq"])
        return {"type": "folder", "id": folder["id"], "sequence_id": seq, "etag": seq, "name": folder["name"]}

    def path_entries(self, record: Record) -> list[Record]:
        chain = list(reversed(tree.ancestors(self.ctx, record)))  # the owner's root first
        trash_at = next((i for i, a in enumerate(chain) if a.get("_trash")), None)
        if trash_at is not None:
            return [dict(ALL_FILES), dict(TRASH), *[self.folder_mini(a) for a in chain[trash_at:]]]
        if record.get("_trash"):
            return [dict(ALL_FILES), dict(TRASH)]
        if record.get("_owner") == self.p.uid:
            return [self.folder_mini(a) for a in chain]
        # A collaborator sees only the contiguous run of ancestors they can reach.
        tail: list[Record] = []
        for ancestor in reversed(chain):
            if db.is_root(ancestor) or not self.p.caps(ancestor):
                break
            tail.append(ancestor)
        tail.reverse()
        return [dict(ALL_FILES), *[self.folder_mini(a) for a in tail]]

    def path_collection(self, record: Record) -> Record:
        entries = self.path_entries(record)
        return {"total_count": len(entries), "entries": entries}

    def parent(self, record: Record) -> Record | None:
        if record.get("_trash"):
            return dict(TRASH)
        parent = tree.parent_of(self.ctx, record)
        if parent is None:
            return None
        if db.is_root(parent):
            return dict(ALL_FILES) if parent["_root_of"] == self.p.uid else None
        if not self.p.caps(parent):
            return None
        return self.folder_mini(parent)

    def item_status(self, record: Record) -> str:
        if record.get("_purged"):
            return "deleted"
        return "trashed" if tree.is_trashed(self.ctx, record) else "active"

    def permissions(self, record: Record) -> Record:
        caps = self.p.caps(record)
        base = {
            "can_download": access.DOWNLOAD in caps,
            "can_upload": access.UPLOAD in caps,
            "can_rename": access.RENAME in caps,
            "can_delete": access.DELETE in caps,
            "can_share": access.SHARE in caps,
            "can_invite_collaborator": access.INVITE in caps,
            "can_set_share_access": access.SET_SHARE_ACCESS in caps,
        }
        if record["type"] == "folder":
            return base
        return {
            "can_download": base["can_download"],
            "can_preview": access.PREVIEW in caps,
            "can_upload": base["can_upload"],
            "can_comment": access.COMMENT in caps,
            "can_rename": base["can_rename"],
            "can_delete": base["can_delete"],
            "can_share": base["can_share"],
            "can_set_share_access": base["can_set_share_access"],
            "can_invite_collaborator": base["can_invite_collaborator"],
            "can_annotate": access.COMMENT in caps,
            "can_view_annotations_all": access.PREVIEW in caps,
            "can_view_annotations_self": access.PREVIEW in caps,
        }

    def has_collaborations(self, record: Record) -> bool:
        return any(c["item_type"] == record["type"] and c["item_id"] == record["id"] for c in db.collabs(self.ctx))

    def is_externally_owned(self, record: Record) -> bool:
        owner = db.users(self.ctx).get(record.get("_owner", ""))
        return bool(owner and owner.get("_kind") == "external")

    def shared_link(self, record: Record) -> Record | None:
        link = record.get("shared_link")
        if not link or not access.link_is_live(self.ctx, record):
            return None
        perms = link["permissions"]
        effective = "can_edit" if perms.get("can_edit") else "can_download" if perms.get("can_download") else "can_preview"
        ext = extension_of(record["name"]) if record["type"] == "file" else ""
        download = (
            self.host_url("app.box.com", f"/shared/static/{link['hash']}{'.' + ext if ext else ''}")
            if record["type"] == "file" and perms.get("can_download")
            else None
        )
        vanity = link.get("vanity_name")
        return {
            "url": self.host_url("app.box.com", f"/s/{link['hash']}"),
            "download_url": download,
            "vanity_url": self.host_url("app.box.com", f"/v/{vanity}") if vanity else None,
            "vanity_name": vanity,
            "access": link["access"],
            "effective_access": link["access"],
            "effective_permission": effective,
            "unshared_at": box_ts(link.get("unshared_at")),
            "is_password_enabled": bool(link.get("password")),
            "permissions": {
                "can_download": bool(perms.get("can_download", True)),
                "can_preview": True,
                "can_edit": bool(perms.get("can_edit", False)),
            },
            "download_count": int(link.get("download_count", 0)),
            "preview_count": int(link.get("preview_count", 0)),
        }

    def collections_of(self, record: Record) -> list[Record]:
        out = []
        for coll in self.ctx.store.collection(db.COLLECTIONS):
            if coll["user_id"] == self.p.uid and record["id"] in coll["items"].get(record["type"], []):
                out.append({"type": "collection", "id": coll["id"], "name": coll["name"], "collection_type": coll["collection_type"]})
        return out

    def metadata_field(self, record: Record, requested: list[str] | None) -> Record:
        from emulators.providers.box import metadata as md

        wanted = [f for f in (requested or []) if f == "metadata" or f.startswith("metadata.")]
        out: Record = {}
        enterprise_scope = md.enterprise_scope(self.ctx)
        for instance in md.instances_for(self.ctx, record):
            scope, key = instance["scope"], instance["template"]
            match = False
            for field in wanted:
                parts = field.split(".")
                if len(parts) == 1:
                    match = True
                elif len(parts) >= 3:
                    want_scope = enterprise_scope if parts[1] == "enterprise" else parts[1]
                    match = match or (want_scope == scope and parts[2] == key)
            if match:
                out.setdefault(scope, {})[key] = md.render_instance(self.ctx, instance)
        return out

    def lock(self, record: Record) -> Record | None:
        lock = record.get("lock")
        if not lock:
            return None
        return {
            "type": "lock",
            "id": lock["id"],
            "created_by": self.user_mini(lock["created_by"]),
            "created_at": box_ts(lock["created_at"]),
            "expired_at": box_ts(lock.get("expired_at")),
            "is_download_prevented": bool(lock.get("is_download_prevented", False)),
            "app_type": None,
        }

    # -- files -------------------------------------------------------------------------
    def file_version_mini(self, record: Record) -> Record | None:
        version = db.versions(self.ctx).get(record.get("_version"))
        if version is None:
            return None
        return {"type": "file_version", "id": version["id"], "sha1": version["sha1"]}

    def file_mini(self, record: Record) -> Record:
        seq = str(record["_seq"])
        return {
            "type": "file",
            "id": record["id"],
            "file_version": self.file_version_mini(record),
            "sequence_id": seq,
            "etag": seq,
            "sha1": record["sha1"],
            "name": record["name"],
        }

    def file(self, record: Record, fields: list[str] | None = None) -> Record:
        seq = str(record["_seq"])
        version = db.versions(self.ctx).get(record.get("_version")) or {}
        owner = record.get("_owner")
        thunks: Thunks = {
            "type": lambda: "file",
            "id": lambda: record["id"],
            "file_version": lambda: self.file_version_mini(record),
            "sequence_id": lambda: seq,
            "etag": lambda: seq,
            "sha1": lambda: record["sha1"],
            "name": lambda: record["name"],
            "description": lambda: record.get("description", ""),
            "size": lambda: record["size"],
            "path_collection": lambda: self.path_collection(record),
            "created_at": lambda: box_ts(record["created_at"]),
            "modified_at": lambda: box_ts(record["modified_at"]),
            "trashed_at": lambda: box_ts(record.get("trashed_at")),
            "purged_at": lambda: box_ts(record.get("purged_at")),
            "content_created_at": lambda: box_ts(record.get("content_created_at")),
            "content_modified_at": lambda: box_ts(record.get("content_modified_at")),
            "created_by": lambda: self.user_mini(record.get("created_by")),
            "modified_by": lambda: self.user_mini(record.get("modified_by")),
            "owned_by": lambda: self.user_mini(owner),
            "shared_link": lambda: self.shared_link(record),
            "parent": lambda: self.parent(record),
            "item_status": lambda: self.item_status(record),
            "extension": lambda: extension_of(record["name"]),
            "version_number": lambda: str(version.get("number", 1)),
            "comment_count": lambda: sum(1 for c in self.ctx.store.collection(db.COMMENTS) if c["item_id"] == record["id"] and c["item_type"] == "file"),
            "permissions": lambda: self.permissions(record),
            "tags": lambda: list(record.get("tags", [])),
            "lock": lambda: self.lock(record),
            "has_collaborations": lambda: self.has_collaborations(record),
            "is_externally_owned": lambda: self.is_externally_owned(record),
            "expires_at": lambda: box_ts(record.get("expires_at")),
            "allowed_invitee_roles": lambda: list(access.ROLES[:2] + ("previewer", "co-owner")) if access.INVITE in self.p.caps(record) else [],
            "watermark_info": lambda: {"is_watermarked": False, "is_watermark_inherited": False},
            "is_accessible_via_shared_link": lambda: bool(self.shared_link(record)),
            "collections": lambda: self.collections_of(record),
            "metadata": lambda: self.metadata_field(record, fields),
            "representations": lambda: self.representations(record),
            "classification": lambda: None,
            "uploader_display_name": lambda: (self.user_mini(record.get("created_by")) or {}).get("name"),
            "disposition_at": lambda: box_ts(record.get("disposition_at")),
            "shared_link_permission_options": lambda: ["can_preview", "can_download", "can_edit"],
            "is_package": lambda: False,
        }
        return build(pick(FILE_ORDER, fields, FILE_STANDARD, FILE_MINI), thunks)

    def representations(self, record: Record) -> Record:
        base = f"/2.0/internal_files/{record['id']}/versions/{record.get('_version')}/representations"
        return {"entries": [], "info": {"url": self.host_url("api.box.com", base)}}

    # -- folders -----------------------------------------------------------------------
    def item_collection(self, folder: Record, limit: int = 100) -> Record:
        children = sorted_children(self.p, folder)
        return {
            "total_count": len(children),
            "entries": [self.item_mini(c) for c in children[:limit]],
            "offset": 0,
            "limit": limit,
            "order": list(FOLDER_ITEM_ORDER),
        }

    def folder(self, record: Record, fields: list[str] | None = None) -> Record:
        seq = None if db.is_root(record) else str(record["_seq"])
        root = db.is_root(record)
        upload = record.get("folder_upload_email")
        thunks: Thunks = {
            "type": lambda: "folder",
            "id": lambda: db.ROOT_WIRE_ID if root else record["id"],
            "sequence_id": lambda: seq,
            "etag": lambda: seq,
            "name": lambda: "All Files" if root else record["name"],
            "created_at": lambda: None if root else box_ts(record["created_at"]),
            "modified_at": lambda: None if root else box_ts(record["modified_at"]),
            "description": lambda: record.get("description", ""),
            "size": lambda: self.p.tree.size(record["id"]),
            "path_collection": lambda: {"total_count": 0, "entries": []} if root else self.path_collection(record),
            "created_by": lambda: None if root else self.user_mini(record.get("created_by")),
            "modified_by": lambda: self.user_mini(record.get("modified_by") or record.get("_owner")),
            "trashed_at": lambda: box_ts(record.get("trashed_at")),
            "purged_at": lambda: box_ts(record.get("purged_at")),
            "content_created_at": lambda: None if root else box_ts(record.get("content_created_at")),
            "content_modified_at": lambda: None if root else box_ts(record.get("content_modified_at")),
            "owned_by": lambda: self.user_mini(record.get("_owner")),
            "shared_link": lambda: self.shared_link(record),
            "folder_upload_email": lambda: dict(upload) if upload else None,
            "parent": lambda: None if root else self.parent(record),
            "item_status": lambda: self.item_status(record),
            "item_collection": lambda: self.item_collection(record),
            "sync_state": lambda: record.get("sync_state", "not_synced"),
            "has_collaborations": lambda: self.has_collaborations(record),
            "permissions": lambda: self.permissions(record),
            "tags": lambda: list(record.get("tags", [])),
            "can_non_owners_invite": lambda: bool(record.get("can_non_owners_invite", True)),
            "is_externally_owned": lambda: self.is_externally_owned(record),
            "is_collaboration_restricted_to_enterprise": lambda: bool(
                record.get("is_collaboration_restricted_to_enterprise", False)
            ),
            "allowed_shared_link_access_levels": lambda: ["open", "company", "collaborators"],
            "allowed_invitee_roles": lambda: list(access.ROLES) if access.INVITE in self.p.caps(record) else [],
            "watermark_info": lambda: {"is_watermarked": False, "is_watermark_inherited": False},
            "is_accessible_via_shared_link": lambda: bool(self.shared_link(record)),
            "can_non_owners_view_collaborators": lambda: bool(record.get("can_non_owners_view_collaborators", True)),
            "classification": lambda: None,
            "collections": lambda: self.collections_of(record),
            "metadata": lambda: self.metadata_field(record, fields),
            "expires_at": lambda: None,
        }
        return build(pick(FOLDER_ORDER, fields, FOLDER_STANDARD, FOLDER_MINI), thunks)

    # -- web links ---------------------------------------------------------------------
    def web_link(self, record: Record, fields: list[str] | None = None) -> Record:
        seq = str(record["_seq"])
        thunks: Thunks = {
            "type": lambda: "web_link",
            "id": lambda: record["id"],
            "sequence_id": lambda: seq,
            "etag": lambda: seq,
            "name": lambda: record["name"],
            "url": lambda: record["url"],
            "created_at": lambda: box_ts(record["created_at"]),
            "modified_at": lambda: box_ts(record["modified_at"]),
            "description": lambda: record.get("description", ""),
            "path_collection": lambda: self.path_collection(record),
            "created_by": lambda: self.user_mini(record.get("created_by")),
            "modified_by": lambda: self.user_mini(record.get("modified_by")),
            "trashed_at": lambda: box_ts(record.get("trashed_at")),
            "purged_at": lambda: box_ts(record.get("purged_at")),
            "shared_link": lambda: self.shared_link(record),
            "parent": lambda: self.parent(record),
            "item_status": lambda: self.item_status(record),
        }
        return build(pick(WEB_LINK_ORDER, fields, WEB_LINK_ORDER, WEB_LINK_MINI), thunks)

    # -- generic items -----------------------------------------------------------------
    def item_mini(self, record: Record) -> Record:
        if record["type"] == "file":
            return self.file_mini(record)
        if record["type"] == "web_link":
            return self.web_link(record, ["url"])
        return self.folder_mini(record)

    def item(self, record: Record, fields: list[str] | None = None, *, listing: bool = False) -> Record:
        """``listing`` renders folder-list entries: mini unless ``fields`` ask for more."""
        if record["type"] == "file":
            return self.file(record, fields) if fields or not listing else self.file_mini(record)
        if record["type"] == "web_link":
            return self.web_link(record, fields) if fields or not listing else self.item_mini(record)
        if fields or not listing:
            if listing and fields:
                return self.folder(record, [f for f in fields if f != "item_collection"] or ["name"])
            return self.folder(record, fields)
        return self.folder_mini(record)

    # -- collaborations ----------------------------------------------------------------
    def accessible_by(self, collab: Record) -> Record | None:
        if collab["accessible_type"] == "group":
            group = db.groups(self.ctx).get(collab["accessible_id"])
            return self.group_mini(group) if group else None
        return self.user_mini(collab["accessible_id"])

    def collaboration(self, collab: Record, fields: list[str] | None = None) -> Record:
        record = db.item(self.ctx, collab["item_type"], collab["item_id"])
        thunks: Thunks = {
            "type": lambda: "collaboration",
            "id": lambda: collab["id"],
            "created_by": lambda: self.user_mini(collab.get("created_by")),
            "created_at": lambda: box_ts(collab["created_at"]),
            "modified_at": lambda: box_ts(collab["modified_at"]),
            "expires_at": lambda: box_ts(collab.get("expires_at")),
            "status": lambda: collab["status"],
            "accessible_by": lambda: self.accessible_by(collab),
            "invite_email": lambda: collab.get("invite_email"),
            "role": lambda: collab["role"],
            "acknowledged_at": lambda: box_ts(collab.get("acknowledged_at")),
            "item": lambda: self.item_mini(record) if record and not record.get("_purged") else None,
            "is_access_only": lambda: bool(collab.get("is_access_only", False)),
        }
        order = tuple(thunks)
        mini = ("type", "id")
        return build(pick(order, fields, order, mini), thunks)

    # -- versions ----------------------------------------------------------------------
    def version(self, version: Record, fields: list[str] | None = None) -> Record:
        thunks: Thunks = {
            "type": lambda: "file_version",
            "id": lambda: version["id"],
            "sha1": lambda: version["sha1"],
            "name": lambda: version["name"],
            "size": lambda: version["size"],
            "created_at": lambda: box_ts(version["created_at"]),
            "modified_at": lambda: box_ts(version["modified_at"]),
            "modified_by": lambda: self.user_mini(version.get("modified_by")),
            "trashed_at": lambda: box_ts(version.get("trashed_at")),
            "trashed_by": lambda: self.user_mini(version.get("trashed_by")),
            "restored_at": lambda: box_ts(version.get("restored_at")),
            "restored_by": lambda: self.user_mini(version.get("restored_by")),
            "purged_at": lambda: box_ts(version.get("purged_at")),
            "uploader_display_name": lambda: (self.user_mini(version.get("modified_by")) or {}).get("name"),
            "version_number": lambda: str(version["number"]),
        }
        order = tuple(thunks)
        return build(pick(order, fields, order, ("type", "id", "sha1")), thunks)


def sorted_children(
    principal: Principal, folder: Record, *, sort: str | None = None, direction: str = "ASC"
) -> list[Record]:
    """Children the caller can see, in Box's order (folders first, then name; or ``sort``)."""
    items = [c for c in principal.tree.children(folder["id"]) if principal.caps(c)]
    if db.is_root(folder) and folder["_root_of"] == principal.uid:
        items += shared_with_me(principal)
    reverse = direction.upper() == "DESC"
    folders_ = [i for i in items if i["type"] != "file"]
    files_ = [i for i in items if i["type"] == "file"]
    if not sort:
        by_name = lambda r: r["name"].lower()  # noqa: E731
        return sorted(folders_, key=by_name, reverse=reverse) + sorted(files_, key=by_name, reverse=reverse)
    keys: dict[str, Callable[[Record], Any]] = {
        "id": lambda r: int(r["id"]),
        "name": lambda r: r["name"].lower(),
        "date": lambda r: r.get("modified_at") or "",
        "size": lambda r: int(r.get("size", 0)) if r["type"] == "file" else principal.tree.size(r["id"]),
    }
    key = keys.get(sort, keys["name"])
    return sorted(folders_, key=key, reverse=reverse) + sorted(files_, key=key, reverse=reverse)


def shared_with_me(principal: Principal) -> list[Record]:
    """Items collaborated with the caller whose parent they cannot otherwise reach.

    Box shows these at the top level ("All Files") of the collaborator's account.
    """
    ctx = principal.ctx
    seen: set[tuple[str, str]] = set()
    out: list[Record] = []
    for (item_type, item_id), _ in principal._collab_index().items():
        record = db.item(ctx, item_type, item_id)
        if record is None or record.get("_trash") or record.get("_owner") == principal.uid or (item_type, item_id) in seen:
            continue
        parent = tree.parent_of(ctx, record)
        if parent is not None and not db.is_root(parent) and principal.raw_caps(parent, with_links=False):
            continue
        if tree.is_trashed(ctx, record):
            continue
        seen.add((item_type, item_id))
        out.append(record)
    return out
