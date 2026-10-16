"""Parse Google's partial-response selectors without flattening arrays or objects."""

from __future__ import annotations

import copy
import re
from typing import Any

from emulators.providers.google.common import google_error

Tree = dict[str, Any]


def leaves(names: str) -> Tree:
    return dict.fromkeys(names.split())


USER = leaves("kind displayName photoLink me permissionId emailAddress")
PERMISSION = {
    **leaves(
        "kind id type role emailAddress domain displayName photoLink allowFileDiscovery expirationTime deleted pendingOwner view"
    ),
    "permissionDetails": leaves("permissionType role inheritedFrom inherited"),
}
FILE = {
    **leaves(
        "kind id name mimeType description starred trashed explicitlyTrashed parents spaces version webContentLink webViewLink iconLink hasThumbnail thumbnailLink thumbnailVersion viewedByMe viewedByMeTime createdTime modifiedTime modifiedByMeTime modifiedByMe sharedWithMeTime driveId teamDriveId shared ownedByMe viewersCanCopyContent copyRequiresWriterPermission writersCanShare permissionIds folderColorRgb originalFilename fullFileExtension fileExtension md5Checksum sha1Checksum sha256Checksum size quotaBytesUsed headRevisionId isAppAuthorized resourceKey hasAugmentedPermissions trashedTime downloadUrl"
    ),
    "properties": {"*": None},
    "appProperties": {"*": None},
    "exportLinks": {"*": None},
    "owners": USER,
    "lastModifyingUser": USER,
    "sharingUser": USER,
    "trashingUser": USER,
    "permissions": PERMISSION,
    "capabilities": leaves(
        "canEdit canComment canShare canCopy canDownload canDelete canTrash canUntrash canAddChildren canListChildren canRename canMoveItemWithinDrive canReadRevisions canMoveChildrenWithinDrive canRemoveChildren canDeleteChildren canTrashChildren canChangeViewersCanCopyContent canChangeCopyRequiresWriterPermission canChangeSecurityUpdateEnabled canModifyContent canModifyContentRestriction canModifyLabels canReadLabels canMoveItemOutOfDrive canMoveItemIntoTeamDrive canMoveChildrenOutOfDrive canChangeItemDownloadRestriction canChangeContentRestriction canMoveTeamDriveItem canRemoveContentRestriction"
    ),
    "contentHints": {"indexableText": None, "thumbnail": leaves("image mimeType")},
    "imageMediaMetadata": {
        "location": leaves("latitude longitude altitude"),
        **leaves(
            "width height rotation time cameraMake cameraModel exposureTime aperture flashUsed focalLength isoSpeed meteringMode sensor exposureMode colorSpace whiteBalance exposureBias maxApertureValue subjectDistance lens"
        ),
    },
    "videoMediaMetadata": leaves("width height durationMillis"),
    "shortcutDetails": leaves("targetId targetMimeType targetResourceKey"),
    "contentRestrictions": {
        **leaves("readOnly reason restrictionTime type ownerRestricted systemRestricted"),
        "restrictingUser": USER,
    },
    "linkShareMetadata": leaves("securityUpdateEligible securityUpdateEnabled"),
    "labelInfo": {"labels": {"*": None}},
}
DRIVE = {
    **leaves("kind id name colorRgb backgroundImageLink createdTime hidden themeId orgUnitId"),
    "capabilities": {"*": None},
    "restrictions": {"*": None},
    "backgroundImageFile": leaves("id xCoordinate yCoordinate width"),
}
REVISION = {
    **leaves(
        "kind id mimeType modifiedTime keepForever originalFilename size md5Checksum published publishAuto publishedOutsideDomain publishedLink"
    ),
    "lastModifyingUser": USER,
    "exportLinks": {"*": None},
}
SCHEMAS = {
    "drive#file": FILE,
    "drive#fileList": {**leaves("kind nextPageToken incompleteSearch"), "files": FILE},
    "drive#drive": DRIVE,
    "drive#driveList": {**leaves("kind nextPageToken"), "drives": DRIVE},
    "drive#permission": PERMISSION,
    "drive#permissionList": {**leaves("kind nextPageToken"), "permissions": PERMISSION},
    "drive#revision": REVISION,
    "drive#revisionList": {**leaves("kind nextPageToken"), "revisions": REVISION},
    "drive#changeList": {
        **leaves("kind nextPageToken newStartPageToken"),
        "changes": {**leaves("kind changeType time removed fileId driveId"), "file": FILE, "drive": DRIVE},
    },
    "drive#about": {
        **leaves("kind maxUploadSize appInstalled canCreateDrives canCreateTeamDrives folderColorPalette"),
        "user": USER,
        "storageQuota": leaves("limit usage usageInDrive usageInDriveTrash"),
        "exportFormats": {"*": None},
        "importFormats": {"*": None},
    },
}


def validate(tree: Tree, schema: Tree) -> None:
    for name, child in tree.items():
        if name == "*":
            continue
        if name not in schema and "*" not in schema:
            raise google_error(
                400, "Invalid field selection " + name, reason="invalidParameter", location="fields"
            )
        nested = schema.get(name, schema.get("*"))
        if child is not None:
            if not isinstance(nested, dict):
                raise google_error(
                    400, "Invalid field selection " + name, reason="invalidParameter", location="fields"
                )
            validate(child, nested)


def needs_binary_metadata(fields: str | None) -> bool:
    if fields is None:
        return False
    tree = parse_fields(fields)
    names = {"size", "quotaBytesUsed", "md5Checksum", "sha1Checksum", "sha256Checksum", "headRevisionId"}

    def walk(node: Tree) -> bool:
        return (
            bool(names.intersection(node))
            or "*" in node
            or any(
                (key == "files" and child is None) or (isinstance(child, dict) and walk(child))
                for key, child in node.items()
            )
        )

    return walk(tree)


def public(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [public(v) for v in value]
    return copy.deepcopy(value)


def parse_fields(text: str) -> Tree:
    tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*|[*/(),]|\S", text)
    pos = 0

    def merge(target: Tree, other: Tree) -> None:
        for key, value in other.items():
            if key not in target:
                target[key] = value
            elif target[key] is None or value is None:
                target[key] = None
            else:
                merge(target[key], value)

    def selectors(nested: bool = False) -> Tree:
        nonlocal pos
        result: Tree = {}
        while True:
            path = []
            while True:
                if pos >= len(tokens) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*|\*", tokens[pos]):
                    raise ValueError
                path.append(tokens[pos])
                pos += 1
                if pos < len(tokens) and tokens[pos] == "/":
                    pos += 1
                else:
                    break
            child = None
            if pos < len(tokens) and tokens[pos] == "(":
                pos += 1
                child = selectors(True)
            branch = child
            for segment in reversed(path):
                branch = {segment: branch}
            merge(result, branch)
            if pos == len(tokens):
                if nested:
                    raise ValueError
                return result
            if nested and tokens[pos] == ")":
                pos += 1
                return result
            if tokens[pos] != ",":
                raise ValueError
            pos += 1

    try:
        return selectors()
    except ValueError:
        raise google_error(
            400, "Invalid field selection " + text, reason="invalidParameter", location="fields"
        ) from None


def project(value: Any, tree: Tree) -> Any:
    if isinstance(value, list):
        return [project(item, tree) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = public(value) if "*" in tree else {}
    for key, child in tree.items():
        if key in value and not key.startswith("_"):
            result[key] = public(value[key]) if child is None else project(value[key], child)
    return result


def select(value: dict[str, Any], fields: str | None, default: str = "*") -> dict[str, Any]:
    tree = parse_fields(default if fields is None else fields)
    schema = SCHEMAS.get(value.get("kind", ""))
    if schema is not None:
        from emulators.providers.google.api_schema import SCHEMAS as DISCOVERY_SCHEMAS
        from emulators.providers.google.wire import field_tree

        name = value["kind"].split("#")[-1]
        reference = "drive:" + name[0].upper() + name[1:]
        if reference in DISCOVERY_SCHEMAS:
            schema = field_tree(DISCOVERY_SCHEMAS[reference])
        validate(tree, schema)
    return project(value, tree)
