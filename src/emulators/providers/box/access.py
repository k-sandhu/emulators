"""Who is calling and what they may do: tokens, ``As-User``, shared links, roles and visibility.

Box has no way to break permission inheritance, so access to an item is the union of

* ownership (the item lives in the user's own tree),
* accepted, unexpired collaborations on the item or any ancestor folder (directly or via a
  group the user belongs to), and
* an item-scoped shared link presented in the ``BoxApi`` header.

Items the caller cannot see at all are reported as ``404 not_found`` (Box hides their
existence); items they can see but not change give ``403 access_denied_insufficient_permissions``.
Downscoped tokens (``restricted_to``) further intersect the result with the token's scopes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import utc
from emulators.core.provider import ProviderContext
from emulators.providers.box import db, errors, tree
from emulators.providers.box.util import try_parse_ts

Record = dict[str, Any]

# Capabilities derived from roles.
PREVIEW, DOWNLOAD, UPLOAD, COMMENT = "preview", "download", "upload", "comment"
RENAME, DELETE, SHARE, SET_SHARE_ACCESS, INVITE = "rename", "delete", "share", "set_share_access", "invite"
ALL_CAPS = frozenset({PREVIEW, DOWNLOAD, UPLOAD, COMMENT, RENAME, DELETE, SHARE, SET_SHARE_ACCESS, INVITE})
NO_CAPS: frozenset[str] = frozenset()

ROLES = (
    "editor",
    "viewer",
    "previewer",
    "uploader",
    "previewer uploader",
    "viewer uploader",
    "co-owner",
)

ROLE_CAPS: dict[str, frozenset[str]] = {
    "owner": ALL_CAPS,
    "co-owner": ALL_CAPS,
    "editor": frozenset(
        {PREVIEW, DOWNLOAD, UPLOAD, COMMENT, RENAME, DELETE, SHARE, SET_SHARE_ACCESS, INVITE}
    ),
    "viewer uploader": frozenset({PREVIEW, DOWNLOAD, UPLOAD, COMMENT, SHARE}),
    "previewer uploader": frozenset({PREVIEW, UPLOAD}),
    "viewer": frozenset({PREVIEW, DOWNLOAD, COMMENT, SHARE}),
    "previewer": frozenset({PREVIEW}),
    "uploader": frozenset({UPLOAD}),
}

#: Downscoped-token scopes -> capabilities they allow.
SCOPE_CAPS: dict[str, frozenset[str]] = {
    "root_readwrite": ALL_CAPS,
    "root_readonly": frozenset({PREVIEW, DOWNLOAD}),
    "item_preview": frozenset({PREVIEW}),
    "item_download": frozenset({PREVIEW, DOWNLOAD}),
    "item_upload": frozenset({UPLOAD}),
    "item_readonly": frozenset({PREVIEW, DOWNLOAD}),
    "item_readwrite": frozenset({PREVIEW, DOWNLOAD, UPLOAD, COMMENT, RENAME, DELETE}),
    "item_share": frozenset({SHARE, SET_SHARE_ACCESS}),
    "item_delete": frozenset({DELETE}),
    "item_rename": frozenset({RENAME}),
    "item_comment": frozenset({COMMENT}),
    "base_explorer": frozenset({PREVIEW}),
    "base_picker": frozenset({PREVIEW}),
    "base_preview": frozenset({PREVIEW}),
    "base_upload": frozenset({UPLOAD}),
    "base_sidebar": frozenset({PREVIEW}),
    "annotation_view_all": frozenset({PREVIEW}),
    "annotation_view_self": frozenset({PREVIEW}),
    "annotation_edit": frozenset({PREVIEW}),
}

ANONYMOUS: Record = {
    "type": "user",
    "id": "anonymous",
    "name": "Anonymous",
    "login": "",
    "role": "user",
    "status": "active",
    "_kind": "anonymous",
}


@dataclass
class LinkGrant:
    """A shared link the caller presented; grants read access beneath ``item``."""

    item: Record
    can_download: bool


@dataclass
class Principal:
    ctx: ProviderContext
    user: Record
    actor: Record
    grant: Grant | None = None
    restricted_to: list[Record] = field(default_factory=list)
    links: list[LinkGrant] = field(default_factory=list)
    _tree: tree.Tree | None = field(default=None, repr=False)
    _groups: frozenset[str] | None = field(default=None, repr=False)
    _index: dict[tuple[str, str], list[Record]] | None = field(default=None, repr=False)
    _caps: dict[tuple[str, str, bool], frozenset[str]] = field(default_factory=dict, repr=False)

    # -- identity ------------------------------------------------------------------------
    @property
    def uid(self) -> str:
        return str(self.user["id"])

    @property
    def anonymous(self) -> bool:
        return self.user["id"] == "anonymous"

    @property
    def is_admin(self) -> bool:
        return self.user.get("role") == "admin"

    @property
    def is_privileged(self) -> bool:
        """Admin, co-admin or the app's service account: may manage the enterprise."""
        return self.user.get("role") in ("admin", "coadmin") or self.user.get("_kind") == "service"

    @property
    def is_external(self) -> bool:
        return self.user.get("_kind") == "external"

    @property
    def groups(self) -> frozenset[str]:
        if self._groups is None:
            self._groups = frozenset(
                m["group_id"] for m in db.memberships(self.ctx) if m["user_id"] == self.uid
            )
        return self._groups

    @property
    def tree(self) -> tree.Tree:
        if self._tree is None:
            self._tree = tree.Tree(self.ctx)
        return self._tree

    def invalidate(self) -> None:
        """Forget cached trees and capabilities after a write within the same request."""
        self._tree = None
        self._index = None
        self._caps.clear()
        self._groups = None

    # -- roots ---------------------------------------------------------------------------
    @property
    def root(self) -> Record:
        record = db.folders(self.ctx).get(db.root_id(self.uid))
        if record is None:
            raise errors.not_found()
        return record

    # -- capabilities --------------------------------------------------------------------
    def _collab_index(self) -> dict[tuple[str, str], list[Record]]:
        if self._index is None:
            index: dict[tuple[str, str], list[Record]] = {}
            now = self.ctx.clock.now()
            for collab in db.collabs(self.ctx):
                if collab["status"] != "accepted":
                    continue
                expires = try_parse_ts(collab.get("expires_at"))
                if expires and utc(expires) <= now:
                    continue
                target = (collab["accessible_type"], collab["accessible_id"])
                if target == ("user", self.uid) or (target[0] == "group" and target[1] in self.groups):
                    index.setdefault((collab["item_type"], collab["item_id"]), []).append(collab)
            self._index = index
        return self._index

    def _direct_caps(self, record: Record, *, with_links: bool = True) -> frozenset[str]:
        caps: set[str] = set()
        for collab in self._collab_index().get((record["type"], record["id"]), []):
            caps |= ROLE_CAPS.get(collab["role"], NO_CAPS)
        if with_links:
            for link in self.links:
                if link.item["type"] == record["type"] and link.item["id"] == record["id"]:
                    caps |= {PREVIEW}
                    if link.can_download:
                        caps.add(DOWNLOAD)
        return frozenset(caps)

    def raw_caps(self, record: Record, *, with_links: bool = True) -> frozenset[str]:
        """Capabilities from ownership, collaborations and links, ignoring token scoping."""
        key = (record["type"], record["id"], with_links)
        cached = self._caps.get(key)
        if cached is not None:
            return cached
        if db.is_root(record):
            result = ALL_CAPS if record.get("_root_of") == self.uid else NO_CAPS
        elif record.get("_owner") == self.uid and not self.anonymous:
            result = ALL_CAPS
        else:
            caps = set(self._direct_caps(record, with_links=with_links))
            parent = tree.parent_of(self.ctx, record)
            if parent is not None:
                caps |= self.raw_caps(parent, with_links=with_links)
            result = frozenset(caps)
        self._caps[key] = result
        return result

    def caps(self, record: Record) -> frozenset[str]:
        caps = self.raw_caps(record)
        if not self.restricted_to or not caps:
            return caps
        allowed: set[str] = set()
        for restriction in self.restricted_to:
            target = restriction.get("object") or {}
            if target.get("id") and self._within(record, target):
                allowed |= SCOPE_CAPS.get(restriction.get("scope", ""), NO_CAPS)
            elif not target and restriction.get("scope"):
                allowed |= SCOPE_CAPS.get(restriction["scope"], NO_CAPS)
        return caps & allowed

    def _within(self, record: Record, target: Record) -> bool:
        if record["type"] == target.get("type") and record["id"] == str(target["id"]):
            return True
        return any(
            a["type"] == target.get("type") and a["id"] == str(target["id"])
            for a in tree.ancestors(self.ctx, record)
        )

    def role(self, record: Record) -> str | None:
        """Best-effort display role for permissions; ``owner`` when the user owns the item."""
        if record.get("_owner") == self.uid:
            return "owner"
        best: str | None = None
        order = [
            "previewer",
            "uploader",
            "previewer uploader",
            "viewer",
            "viewer uploader",
            "editor",
            "co-owner",
        ]
        for item in [record, *tree.ancestors(self.ctx, record)]:
            for collab in self._collab_index().get((item["type"], item["id"]), []):
                if best is None or order.index(collab["role"]) > order.index(best):
                    best = collab["role"]
        return best

    def visible(self, record: Record) -> bool:
        """Can the caller see the item at all? Upload-only roles see a folder but not files."""
        caps = self.caps(record)
        if record["type"] == "folder":
            return bool(caps)
        return PREVIEW in caps or DOWNLOAD in caps

    # -- item lookup ---------------------------------------------------------------------
    def folder_record(self, folder_id: str) -> Record | None:
        if folder_id == db.ROOT_WIRE_ID:
            return self.root
        if folder_id.startswith(db.ROOT_PREFIX):
            return None
        return db.folders(self.ctx).get(folder_id)

    def get_item(
        self,
        item_type: str,
        item_id: str,
        need: str | None = None,
        *,
        allow_trashed: bool = False,
        only_trashed: bool = False,
    ) -> Record:
        """Resolve an item the caller may see, else raise Box's 404; ``need`` adds a 403 check."""
        record = (
            self.folder_record(str(item_id))
            if item_type == "folder"
            else db.item(self.ctx, item_type, str(item_id))
        )
        if record is None or record.get("_purged"):
            raise errors.not_found()
        caps = self.caps(record)
        if not self.visible(record):
            raise errors.not_found()
        trashed = tree.is_trashed(self.ctx, record)
        if only_trashed and not record.get("_trash"):
            raise errors.not_found()
        if trashed and not allow_trashed and not only_trashed:
            raise errors.trashed()
        if need and need not in caps:
            raise errors.forbidden()
        return record

    def require(self, record: Record, need: str) -> None:
        if need not in self.caps(record):
            raise errors.forbidden()

    # -- admin ---------------------------------------------------------------------------
    def require_privileged(self) -> None:
        if not self.is_privileged:
            raise errors.forbidden()

    def can_see_user(self, other: Record) -> bool:
        """Managed users see each other; outsiders only through a collaboration; admins see everyone."""
        if self.is_privileged or other["id"] == self.uid:
            return True
        if other.get("_kind") == "service":
            return False
        if self.is_external:
            return any(
                c["accessible_id"] == self.uid and c["created_by"] == other["id"]
                for c in db.collabs(self.ctx)
            )
        if other.get("_kind") != "external":
            return True
        return any(
            c["accessible_type"] == "user"
            and c["accessible_id"] == other["id"]
            and c["created_by"] == self.uid
            for c in db.collabs(self.ctx)
        )


# -- authentication ---------------------------------------------------------------------
def parse_boxapi(request: Request) -> tuple[str, str | None] | None:
    """``BoxApi: shared_link=<url>&shared_link_password=<pw>``."""
    header = request.headers.get("boxapi")
    if not header:
        return None
    parsed = parse_qs(header, keep_blank_values=True)
    link = (parsed.get("shared_link") or [""])[0]
    if not link:
        return None
    return link, (parsed.get("shared_link_password") or [None])[0]


def find_shared_link(ctx: ProviderContext, url: str) -> Record | None:
    """The file/folder whose shared link matches ``url`` (``/s/<hash>`` or a vanity ``/v/<name>``)."""
    path = urlsplit(url).path.rstrip("/")
    token = path.rsplit("/", 1)[-1]
    if not token:
        return None
    for collection in (db.files(ctx), db.folders(ctx)):
        for record in collection:
            link = record.get("shared_link")
            if link and (link["hash"] == token or link.get("vanity_name") == token):
                return record
    return None


def link_is_live(ctx: ProviderContext, record: Record) -> bool:
    link = record.get("shared_link")
    if not link:
        return False
    expires = try_parse_ts(link.get("unshared_at"))
    return not (expires and utc(expires) <= ctx.clock.now())


def resolve_link(ctx: ProviderContext, request: Request, user: Record | None) -> LinkGrant | None:
    parsed = parse_boxapi(request)
    if parsed is None:
        return None
    url, password = parsed
    record = find_shared_link(ctx, url)
    if record is None or not link_is_live(ctx, record) or tree.is_trashed(ctx, record):
        raise errors.not_found()
    link = record["shared_link"]
    access = link["access"]
    if access == "collaborators":
        return None
    if access == "company":
        if user is None:
            raise errors.unauthorized_missing()
        if user.get("_kind") not in ("managed", "service"):
            raise errors.not_found()
    if link.get("password") and password != link["password"]:
        raise errors.forbidden()
    return LinkGrant(record, bool(link["permissions"].get("can_download", True)))


def authenticate(request: Request, ctx: ProviderContext, *, allow_anonymous: bool = False) -> Principal:
    """Resolve the bearer token (and ``As-User`` / ``BoxApi``) into a :class:`Principal`."""
    token = bearer_token(request) or request.query_params.get("access_token")
    grant = ctx.tokens.resolve(token) if token else None
    if grant is None:
        if token:
            raise errors.unauthorized()
        link = resolve_link(ctx, request, None) if allow_anonymous else None
        if link is None:
            raise errors.unauthorized_missing()
        return Principal(ctx, dict(ANONYMOUS), dict(ANONYMOUS), links=[link])
    actor = db.users(ctx).get(grant.user_id) if grant.user_id else None
    if actor is None or actor.get("status") != "active":
        raise errors.unauthorized()
    user = actor
    target_id = request.headers.get("as-user")
    if target_id:
        if actor.get("role") not in ("admin", "coadmin") and actor.get("_kind") != "service":
            raise errors.forbidden()
        target = db.users(ctx).get(target_id.strip())
        if target is None or target.get("_kind") == "external":
            raise errors.not_found()
        if target.get("status") != "active":
            raise errors.forbidden()
        user = target
    principal = Principal(
        ctx, user, actor, grant=grant, restricted_to=list(grant.extra.get("restricted_to") or [])
    )
    link = resolve_link(ctx, request, user)
    if link is not None:
        principal.links.append(link)
    return principal


def lookup_user(ctx: ProviderContext, identifier: str) -> Record | None:
    """A Box user by box id, world person id, or login/email."""
    record = db.users(ctx).get(identifier)
    if record is not None:
        return record
    record = db.user_by_pid(ctx, identifier)
    if record is not None:
        return record
    return db.user_by_login(ctx, identifier)
