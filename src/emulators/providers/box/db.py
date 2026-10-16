"""Collection names, id conventions and typed lookups into the provider store."""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext
from emulators.core.store import Collection

Record = dict[str, Any]

USERS = "box_users"
GROUPS = "box_groups"
MEMBERSHIPS = "box_group_memberships"
FOLDERS = "box_folders"
FILES = "box_files"
VERSIONS = "box_file_versions"
COLLABS = "box_collaborations"
COMMENTS = "box_comments"
TASKS = "box_tasks"
ASSIGNMENTS = "box_task_assignments"
COLLECTIONS = "box_collections"
EVENTS = "box_events"
TEMPLATES = "box_metadata_templates"
METADATA = "box_metadata_instances"
WEBHOOKS = "box_webhooks"
UPLOADS = "box_upload_sessions"
DOWNLOADS = "box_download_tokens"
RECENTS = "box_recent_items"
WEB_LINKS = "box_web_links"
ZIPS = "box_zip_downloads"
CHANNELS = "box_longpoll_channels"
FOLDER_LOCKS = "box_folder_locks"
INVITES = "box_invites"

ROOT_PREFIX = "root_"
ROOT_WIRE_ID = "0"

#: First runtime-minted ids (seeded ids are derived from the world and live elsewhere).
FILE_ID_START = 5_000_000_000_000
FOLDER_ID_START = 500_000_000_000
USER_ID_START = 40_000_000_000
SMALL_ID_START = 100_000_000


def users(ctx: ProviderContext) -> Collection:
    return ctx.store.collection(USERS)


def folders(ctx: ProviderContext) -> Collection:
    return ctx.store.collection(FOLDERS)


def files(ctx: ProviderContext) -> Collection:
    return ctx.store.collection(FILES)


def collabs(ctx: ProviderContext) -> Collection:
    return ctx.store.collection(COLLABS)


def groups(ctx: ProviderContext) -> Collection:
    return ctx.store.collection(GROUPS)


def memberships(ctx: ProviderContext) -> Collection:
    return ctx.store.collection(MEMBERSHIPS)


def versions(ctx: ProviderContext) -> Collection:
    return ctx.store.collection(VERSIONS)


def user_by_login(ctx: ProviderContext, login: str) -> Record | None:
    wanted = login.strip().lower()
    for user in users(ctx):
        if user["login"].lower() == wanted:
            return user
    return None


def user_by_pid(ctx: ProviderContext, pid: str) -> Record | None:
    for user in users(ctx):
        if user.get("_pid") == pid:
            return user
    return None


def root_id(user_id: str) -> str:
    return f"{ROOT_PREFIX}{user_id}"


def is_root(record: Record) -> bool:
    return str(record["id"]).startswith(ROOT_PREFIX)


def item(ctx: ProviderContext, item_type: str, item_id: str) -> Record | None:
    if item_type == "folder":
        return folders(ctx).get(item_id)
    if item_type == "file":
        return files(ctx).get(item_id)
    if item_type == "web_link":
        return ctx.store.collection(WEB_LINKS).get(item_id)
    return None


class Live:
    """A collection handle that survives ``reset``: it re-resolves the collection on every use.

    ``Store.restore`` replaces the collection objects, so route closures must never keep one.
    """

    def __init__(self, ctx: ProviderContext, name: str) -> None:
        self._ctx = ctx
        self._name = name

    def _target(self) -> Collection:
        return self._ctx.store.collection(self._name)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._target(), attr)

    def __iter__(self) -> Any:
        return iter(self._target())

    def __len__(self) -> int:
        return len(self._target())

    def __contains__(self, record_id: object) -> bool:
        return record_id in self._target()
