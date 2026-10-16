"""Static Dropbox catalogs: OAuth scopes, admin roles, team policies, file categories."""

from __future__ import annotations

import mimetypes
from typing import Any

from emulators.providers.dropbox.common import b64url, digest

SCOPES = (
    "account_info.read",
    "account_info.write",
    "contacts.read",
    "contacts.write",
    "events.read",
    "file_requests.read",
    "file_requests.write",
    "files.content.read",
    "files.content.write",
    "files.metadata.read",
    "files.metadata.write",
    "files.permanent_delete",
    "files.team_metadata.read",
    "files.team_metadata.write",
    "groups.read",
    "groups.write",
    "members.delete",
    "members.read",
    "members.write",
    "profile",
    "email",
    "openid",
    "sessions.list",
    "sessions.modify",
    "sharing.read",
    "sharing.write",
    "team_data.governance.read",
    "team_data.governance.write",
    "team_data.member",
    "team_data.content.read",
    "team_data.content.write",
    "team_data.team_space",
    "team_info.read",
    "team_info.write",
)

ROLE_DEFS: list[dict[str, str]] = [
    {
        "key": "team_admin",
        "name": "Team admin",
        "description": "User can do most user provisioning, de-provisioning and management.",
    },
    {
        "key": "user_management_admin",
        "name": "User management admin",
        "description": "User can do all user provisioning, de-provisioning and management.",
    },
    {
        "key": "support_admin",
        "name": "Support admin",
        "description": "User can do access support related tasks for users.",
    },
    {
        "key": "member_only",
        "name": "Member only",
        "description": "User is a regular team member without admin privileges.",
    },
]


def role_id(key: str) -> str:
    return "pid_dbtmr:" + b64url(digest("dbx-role", key))[:39]


def role_obj(key: str) -> dict[str, str]:
    for role in ROLE_DEFS:
        if role["key"] == key:
            return {"role_id": role_id(key), "name": role["name"], "description": role["description"]}
    raise KeyError(key)


DEFAULT_SHARING_POLICIES: dict[str, str] = {
    "shared_folder_member_policy": "anyone",
    "shared_folder_join_policy": "from_anyone",
    "shared_link_create_policy": "default_public",
    "group_creation_policy": "admins_and_members",
    "shared_folder_link_restriction_policy": "anyone",
    "enforce_link_password_policy": "optional",
    "default_link_expiration_days_policy": "none",
    "shared_link_default_permissions_policy": "default",
}


def sharing_policies(raw: dict[str, str]) -> dict[str, Any]:
    return {key: {".tag": value} for key, value in raw.items()}


EXT_CATEGORIES: dict[str, str] = {}
for _ext in ("jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "svg", "ico", "raw", "psd"):
    EXT_CATEGORIES[_ext] = "image"
for _ext in ("doc", "docx", "docm", "txt", "rtf", "odt", "md", "markdown", "html", "htm", "pages", "csv"):
    EXT_CATEGORIES[_ext] = "document"
for _ext in ("xls", "xlsx", "xlsm", "ods", "numbers", "tsv"):
    EXT_CATEGORIES[_ext] = "spreadsheet"
for _ext in ("ppt", "pptx", "pps", "ppsx", "odp", "key"):
    EXT_CATEGORIES[_ext] = "presentation"
EXT_CATEGORIES["pdf"] = "pdf"
EXT_CATEGORIES["paper"] = "paper"
for _ext in ("mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"):
    EXT_CATEGORIES[_ext] = "audio"
for _ext in ("mp4", "mov", "avi", "mkv", "wmv", "webm", "m4v"):
    EXT_CATEGORIES[_ext] = "video"


def category_of(name: str, is_folder: bool) -> str:
    if is_folder:
        return "folder"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return EXT_CATEGORIES.get(ext, "others")


PREVIEWABLE = {
    "doc",
    "docx",
    "docm",
    "ppt",
    "pps",
    "ppsx",
    "ppsm",
    "pptx",
    "pptm",
    "xls",
    "xlsm",
    "xlsx",
    "rtf",
}
THUMBNAILABLE = {"jpg", "jpeg", "png", "tiff", "tif", "gif", "webp", "ppm", "bmp"}


def mime_of(name: str) -> str:
    from emulators.world.documents import EXTENSION_TO_MIME

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in EXTENSION_TO_MIME:
        return EXTENSION_TO_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"
