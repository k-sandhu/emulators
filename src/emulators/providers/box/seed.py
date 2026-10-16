"""Project the shared world into Box: enterprise users, groups, folders, files, sharing.

Mapping (see ``docs/providers/box.md``):

* every employee is a managed user (login = email); Arjun Mehta is the enterprise admin, Sarah
  Johansson the co-admin; departed employees are ``inactive``;
* team spaces are top-level folders owned by the admin, shared with the space's groups and
  people (owner -> co-owner, editor -> editor, viewer -> viewer);
* personal spaces are folders in each employee's own root;
* ``Document.shares`` become collaborations on the file (client contacts are external users,
  some invitations still pending) and link shares become shared links;
* Box cannot break inheritance, so ``Document.restricted`` files live in a ``Restricted`` folder
  of their owner and are shared only with the people named in ``Document.shares``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, time, timedelta
from typing import Any

from emulators.core.ids import stable_guid, stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.box import content, db, events, metadata
from emulators.providers.box.util import derived_number, sha1_hex, to_iso
from emulators.world.model import Document, Person, Share, Space

Record = dict[str, Any]

SERVICE_NAME = "Halcyon Box Automation"
SERVICE_APP_ID = "1982346"
CLIENT_ID = "halcyon-box-app"
DEV_TOKEN_PREFIX = "box-dev-"
WEBHOOK_PRIMARY = "box-primary-signature-key"
WEBHOOK_SECONDARY = "box-secondary-signature-key"

ROLE_MAP = {"owner": "co-owner", "editor": "editor", "viewer": "viewer", "commenter": "viewer"}
ENGAGEMENT_TYPES = [
    "Audit", "Bookkeeping", "Review Engagement", "SR&ED Claim", "Systems Advisory", "Tax Advisory",
    "Tax Compliance", "Transaction Advisory", "Other",
]  # fmt: skip

LANGUAGE = {"en-CA": "en", "en-US": "en", "en-GB": "en", "fr-CA": "fr-CA"}


class Seeder:
    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.world = ctx.world
        self.seed_key = ctx.world.seed
        self.anchor = ctx.world.anchor
        self.used: dict[str, set[int]] = {}
        self.uid: dict[str, str] = {}  # world person id -> box user id
        self.folder_index: dict[tuple[str, str], str] = {}  # (parent id, lowercase name) -> folder id
        self.span: dict[str, list[str]] = {}  # folder id -> [min created, max modified]
        self.file_names: dict[str, set[str]] = {}
        self.file_of_doc: dict[str, str] = {}
        self.admin_uid = ""

    # -- ids ---------------------------------------------------------------------------
    def number(self, kind: str, key: str, low: int, high: int) -> str:
        used = self.used.setdefault(kind, set())
        salt = 0
        while True:
            value = derived_number(self.seed_key, kind, f"{key}/{salt}" if salt else key, low, high)
            if value not in used:
                used.add(value)
                return str(value)
            salt += 1

    def stamp(self, value: datetime) -> str:
        return to_iso(value)

    # -- run ---------------------------------------------------------------------------
    def run(self) -> None:
        self.ctx.store.meta["webhook_keys"] = {"primary": WEBHOOK_PRIMARY, "secondary": WEBHOOK_SECONDARY}
        self.ctx.store.meta["settings"] = {"upload_part_size": 8_388_608}
        self.seed_users()
        self.seed_groups()
        self.seed_files()
        self.seed_space_collaborations()
        self.seed_shares()
        self.seed_collections()
        self.seed_metadata()
        self.seed_social()
        self.seed_dev_tokens()
        self.seed_events()

    # -- users -------------------------------------------------------------------------
    def make_user(self, person: Person | None, **overrides: Any) -> Record:
        world = self.world
        if person is not None:
            created = datetime.combine(person.hire_date or world.org.founded, time(15, 30), UTC)
            office = world.office(person.office_id) if person.office_id else None
            address = office.address.one_line() if office and not person.is_external else ""
            record: Record = {
                "type": "user",
                "id": self.number("user", person.id, 30_000_000_000, 39_999_999_999),
                "name": person.display_name,
                "login": person.email,
                "created_at": self.stamp(created),
                "modified_at": self.stamp(
                    created + timedelta(days=30 + int(stable_hash(person.id, length=2), 16) % 200)
                ),
                "language": LANGUAGE.get(person.locale, person.locale.split("-")[0]),
                "timezone": person.timezone,
                "status": "active" if person.active else "inactive",
                "job_title": person.job_title,
                "phone": person.phone_business or "",
                "address": address,
                "role": "user",
                "_pid": person.id,
                "_kind": "external" if person.is_external else "managed",
            }
        else:
            record = {}
        record.update(overrides)
        record["_root"] = db.root_id(record["id"])
        db.users(self.ctx).put(record)
        db.folders(self.ctx).put(
            {
                "type": "folder",
                "id": record["_root"],
                "name": "All Files",
                "_root_of": record["id"],
                "_owner": record["id"],
                "_parent": None,
                "_seq": 0,
                "created_at": record["created_at"],
                "modified_at": record["modified_at"],
            }
        )
        return record

    def seed_users(self) -> None:
        world = self.world
        referenced: set[str] = set()
        for doc in world.documents:
            referenced.update({doc.owner_id, doc.modified_by_id})
            referenced.update(s.principal_id for s in doc.shares if s.principal_id)
        for person in world.employees:
            record = self.make_user(person)
            self.uid[person.id] = record["id"]
            if person.id == "p035":
                record["role"] = "admin"
                self.admin_uid = record["id"]
            elif person.id == "p036":
                record["role"] = "coadmin"
        for person in world.external_people:
            if person.id in referenced:
                self.uid[person.id] = self.make_user(person)["id"]
        created = datetime.combine(world.org.founded, time(12, 0), UTC)
        self.service = self.make_user(
            None,
            id=self.number("user", "service-account", 30_000_000_000, 39_999_999_999),
            name=SERVICE_NAME,
            login=f"AutomationUser_{SERVICE_APP_ID}_{stable_hash('box-service', length=10)}@boxdevedition.com",
            created_at=self.stamp(created),
            modified_at=self.stamp(created),
            language="en",
            timezone="America/Toronto",
            status="active",
            job_title="",
            phone="",
            address="",
            role="user",
            _pid=None,
            _kind="service",
        )

    # -- groups ------------------------------------------------------------------------
    def seed_groups(self) -> None:
        world = self.world
        self.gid: dict[str, str] = {}
        wanted = [g for g in world.groups if g.kind != "distribution"]
        for group in wanted:
            gid = self.number("group", group.id, 1_000_000, 9_999_999)
            self.gid[group.id] = gid
            created = self.anchor - timedelta(days=700 + int(stable_hash(group.id, length=2), 16))
            db.groups(self.ctx).put(
                {
                    "type": "group",
                    "id": gid,
                    "name": group.name,
                    "group_type": "managed_group",
                    "created_at": self.stamp(created),
                    "modified_at": self.stamp(created),
                    "provenance": None,
                    "external_sync_identifier": None,
                    "description": group.description,
                    "invitability_level": "admins_and_members"
                    if group.kind == "department"
                    else "admins_only",
                    "member_viewability_level": "admins_and_members",
                    "_wid": group.id,
                }
            )
            for member in group.member_ids:
                if member not in self.uid:
                    continue
                mid = self.number("membership", f"{group.id}/{member}", 100_000_000, 999_999_999)
                db.memberships(self.ctx).put(
                    {
                        "type": "group_membership",
                        "id": mid,
                        "user_id": self.uid[member],
                        "group_id": gid,
                        "role": "admin" if member in group.owner_ids else "member",
                        "created_at": self.stamp(created + timedelta(days=1)),
                        "modified_at": self.stamp(created + timedelta(days=1)),
                    }
                )

    # -- folders and files ----------------------------------------------------------------
    def ensure_folder(
        self, owner: str, parent_id: str, name: str, path_key: str, creator: str, created: str
    ) -> str:
        key = (parent_id, name.lower())
        existing = self.folder_index.get(key)
        if existing:
            return existing
        fid = self.number("folder", f"{owner}/{path_key}", 100_000_000_000, 499_999_999_999)
        db.folders(self.ctx).put(
            {
                "type": "folder",
                "id": fid,
                "name": name,
                "description": "",
                "_parent": parent_id,
                "_owner": owner,
                "created_by": creator,
                "modified_by": creator,
                "created_at": created,
                "modified_at": created,
                "content_created_at": created,
                "content_modified_at": created,
                "trashed_at": None,
                "purged_at": None,
                "_trash": False,
                "_seq": 0,
                "shared_link": None,
                "tags": [],
                "folder_upload_email": None,
                "sync_state": "not_synced",
            }
        )
        self.folder_index[key] = fid
        return fid

    def folder_chain(
        self, owner: str, root_parent: str, base_key: str, parts: list[str], creator: str, created: str
    ) -> str:
        parent = root_parent
        path = base_key
        for part in parts:
            path = f"{path}/{part}"
            parent = self.ensure_folder(owner, parent, part, path, creator, created)
        return parent

    def touch_span(self, folder_id: str, created: str, modified: str) -> None:
        span = self.span.get(folder_id)
        if span is None:
            self.span[folder_id] = [created, modified]
        else:
            span[0] = min(span[0], created)
            span[1] = max(span[1], modified)

    def unique_name(self, parent_id: str, name: str) -> str:
        taken = self.file_names.setdefault(parent_id, set())
        candidate = name
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        n = 1
        while candidate.lower() in taken:
            n += 1
            candidate = f"{stem} ({n}){'.' + ext if ext else ''}"
        taken.add(candidate.lower())
        return candidate

    def seed_files(self) -> None:
        world = self.world
        self.team_folder: dict[str, str] = {}
        spaces = {s.id: s for s in world.spaces}
        for space in world.spaces:
            if space.kind != "team":
                continue
            created = self.stamp(self.anchor - timedelta(days=900))
            fid = self.ensure_folder(
                self.admin_uid,
                db.root_id(self.admin_uid),
                space.name,
                f"team/{space.id}",
                self.admin_uid,
                created,
            )
            self.team_folder[space.id] = fid
            folder = db.folders(self.ctx).get(fid)
            folder["description"] = space.description  # type: ignore[index]
        for doc in world.documents:
            self.seed_document(doc, spaces[doc.space_id])
        self.finish_folder_times()

    def doc_location(self, doc: Document, space: Space) -> tuple[str, str, str, list[str]]:
        """(owner uid, parent root folder id, key prefix, folder parts) for a document."""
        owner_pid = space.owner_id
        parts = [p for p in doc.folder.split("/") if p]
        if doc.restricted:
            owner = self.uid[doc.owner_id]
            return owner, db.root_id(owner), f"restricted/{space.id}", ["Restricted", space.name, *parts]
        if space.kind == "personal":
            owner = self.uid[owner_pid]
            return owner, db.root_id(owner), f"personal/{space.id}", parts
        return self.admin_uid, self.team_folder[space.id], f"team/{space.id}", parts

    def seed_document(self, doc: Document, space: Space) -> None:
        ctx = self.ctx
        owner, root_parent, key, parts = self.doc_location(doc, space)
        created = self.stamp(doc.created)
        modified = self.stamp(doc.modified)
        creator = self.uid[doc.owner_id]
        parent = self.folder_chain(owner, root_parent, key, parts, creator, created)
        name = self.unique_name(parent, doc.name)
        # Roll file times up through ancestors so folders look as old/new as their contents.
        cursor = db.folders(ctx).get(parent)
        while cursor is not None and not db.is_root(cursor):
            self.touch_span(cursor["id"], created, modified)
            cursor = db.folders(ctx).get(cursor["_parent"]) if cursor.get("_parent") else None
        data = content.render_document(ctx, doc.id)
        blob = ctx.store.blobs.put(data)
        fid = self.number("file", doc.id, 1_000_000_000_000, 4_999_999_999_999)
        self.file_of_doc[doc.id] = fid
        total = max(1, doc.versions)
        version_ids = [
            self.number("version", f"{doc.id}/{n}", 6_000_000_000_000, 8_999_999_999_999)
            for n in range(1, total + 1)
        ]
        span = (doc.modified - doc.created) if total > 1 else timedelta(0)
        for n, vid in enumerate(version_ids, start=1):
            at = doc.created + span * (n - 1) // max(total - 1, 1)
            current = n == total
            db.versions(ctx).put(
                {
                    "type": "file_version",
                    "id": vid,
                    "_file": fid,
                    "number": n,
                    "name": name,
                    "sha1": sha1_hex(data) if current else None,
                    "size": len(data) if current else 0,
                    "_blob": blob if current else None,
                    "created_at": self.stamp(at),
                    "modified_at": self.stamp(at),
                    "modified_by": self.uid[doc.modified_by_id] if current else creator,
                    "trashed_at": None,
                    "trashed_by": None,
                    "restored_at": None,
                    "restored_by": None,
                    "purged_at": None,
                    "_doc": doc.id,
                    "_total": total,
                }
            )
        link = self.shared_link_for(doc)
        db.files(ctx).put(
            {
                "type": "file",
                "id": fid,
                "name": name,
                "description": doc.description,
                "_parent": parent,
                "_owner": owner,
                "created_by": creator,
                "modified_by": self.uid[doc.modified_by_id],
                "created_at": created,
                "modified_at": modified,
                "content_created_at": created,
                "content_modified_at": modified,
                "trashed_at": modified if doc.trashed else None,
                "purged_at": None,
                "_trash": bool(doc.trashed),
                "_seq": total - 1,
                "shared_link": link,
                "tags": [],
                "lock": None,
                "expires_at": None,
                "sha1": sha1_hex(data),
                "size": len(data),
                "_blob": blob,
                "_version": version_ids[-1],
                "_doc": doc.id,
                "_text": content.search_text(doc.paragraphs, doc.title, doc.table),
            }
        )

    def shared_link_for(self, doc: Document) -> Record | None:
        for share in doc.shares:
            if share.link == "none":
                continue
            digest = hashlib.sha256(f"{self.seed_key}/link/{doc.id}".encode()).hexdigest()
            return {
                "hash": digest[:32],
                "access": "open" if share.link == "anyone" else "company",
                "password": None,
                "unshared_at": self.stamp(share.expires) if share.expires else None,
                "permissions": {
                    "can_download": True,
                    "can_preview": True,
                    "can_edit": share.role == "editor",
                },
                "download_count": 0,
                "preview_count": 0,
                "vanity_name": None,
                "created_at": self.stamp(share.created or doc.created),
                "created_by": self.uid[doc.owner_id],
            }
        return None

    def finish_folder_times(self) -> None:
        for fid, (created, modified) in self.span.items():
            folder = db.folders(self.ctx).get(fid)
            if folder is None:
                continue
            folder["created_at"] = folder["content_created_at"] = created
            folder["modified_at"] = folder["content_modified_at"] = modified

    # -- collaborations ----------------------------------------------------------------
    def collab(
        self,
        key: str,
        item_type: str,
        item_id: str,
        target_type: str,
        target_id: str,
        role: str,
        created_by: str,
        created: str,
        *,
        status: str = "accepted",
        expires: str | None = None,
        invite_email: str | None = None,
    ) -> Record:
        cid = self.number("collab", key, 10_000_000_000, 99_999_999_999)
        record = {
            "type": "collaboration",
            "id": cid,
            "item_type": item_type,
            "item_id": item_id,
            "accessible_type": target_type,
            "accessible_id": target_id,
            "role": role,
            "status": status,
            "created_by": created_by,
            "created_at": created,
            "modified_at": created,
            "expires_at": expires,
            "invite_email": invite_email,
            "acknowledged_at": self.stamp(datetime.fromisoformat(created) + timedelta(days=1))
            if status == "accepted"
            else None,
            "is_access_only": False,
        }
        db.collabs(self.ctx).put(record)
        return record

    def seed_space_collaborations(self) -> None:
        created = self.stamp(self.anchor - timedelta(days=880))
        for space in self.world.spaces:
            if space.kind != "team":
                continue
            folder_id = self.team_folder[space.id]
            seen_owner = False
            for principal, role in space.members:
                box_role = ROLE_MAP[role]
                if principal in self.gid:
                    self.collab(
                        f"space/{space.id}/{principal}",
                        "folder",
                        folder_id,
                        "group",
                        self.gid[principal],
                        box_role,
                        self.admin_uid,
                        created,
                    )
                elif principal in self.uid:
                    seen_owner = seen_owner or principal == space.owner_id
                    self.collab(
                        f"space/{space.id}/{principal}",
                        "folder",
                        folder_id,
                        "user",
                        self.uid[principal],
                        box_role,
                        self.admin_uid,
                        created,
                    )
            # Like a real integration, the app's service account is a co-owner of every team folder.
            self.collab(
                f"space/{space.id}/service", "folder", folder_id, "user", self.service["id"], "co-owner",
                self.admin_uid, created,
            )  # fmt: skip
            owner_uid = self.uid.get(space.owner_id)
            if owner_uid and owner_uid != self.admin_uid and not seen_owner:
                self.collab(
                    f"space/{space.id}/owner",
                    "folder",
                    folder_id,
                    "user",
                    owner_uid,
                    "co-owner",
                    self.admin_uid,
                    created,
                )

    def seed_shares(self) -> None:
        for doc in self.world.documents:
            file_id = self.file_of_doc[doc.id]
            inviter = self.uid[doc.owner_id]
            for share in doc.shares:
                if share.link != "none" or not share.principal_id or share.principal_id not in self.uid:
                    continue
                self.seed_share(doc, file_id, inviter, share)

    def seed_share(self, doc: Document, file_id: str, inviter: str, share: Share) -> None:
        assert share.principal_id
        person = self.world.person(share.principal_id)
        created = self.stamp(share.created or doc.created)
        pending = (
            person.is_external
            and doc.modified_by_id != person.id
            and int(stable_hash(doc.id, person.id, length=4), 16) % 4 == 0
        )
        user_id = self.uid[share.principal_id]
        self.collab(
            f"share/{doc.id}/{share.principal_id}",
            "file",
            file_id,
            "user",
            user_id,
            ROLE_MAP[share.role],
            inviter,
            created,
            status="pending" if pending else "accepted",
            expires=self.stamp(share.expires) if share.expires else None,
            invite_email=person.email if pending else None,
        )

    # -- favorites ---------------------------------------------------------------------
    def seed_collections(self) -> None:
        for user in db.users(self.ctx):
            cid = self.number("collection", user["id"], 10_000_000, 99_999_999)
            self.ctx.store.collection(db.COLLECTIONS).put(
                {
                    "type": "collection",
                    "id": cid,
                    "user_id": user["id"],
                    "name": "Favorites",
                    "collection_type": "favorites",
                    "items": {"file": [], "folder": []},
                }
            )
        for doc in self.world.documents:
            if not doc.starred:
                continue
            user_id = self.uid.get(doc.owner_id)
            coll = next(
                (c for c in self.ctx.store.collection(db.COLLECTIONS) if c["user_id"] == user_id), None
            )
            if coll is not None:
                coll["items"]["file"].append(self.file_of_doc[doc.id])

    # -- metadata ----------------------------------------------------------------------
    def seed_metadata(self) -> None:
        ctx = self.ctx
        scope = metadata.enterprise_scope(ctx)
        template = {
            "id": "f4a1c0de-6d20-4f54-9c7a-0b1e2d3c4a5b",
            "type": "metadata_template",
            "templateKey": "clientInfo",
            "scope": scope,
            "displayName": "Client Info",
            "hidden": False,
            "copyInstanceOnItemCopy": False,
            "fields": [
                {
                    "id": "0f6b3b3e-1f0a-4d1c-8d3e-3a1f6a9b0001",
                    "type": "string",
                    "key": "clientName",
                    "displayName": "Client Name",
                    "hidden": False,
                },
                {
                    "id": "0f6b3b3e-1f0a-4d1c-8d3e-3a1f6a9b0002",
                    "type": "enum",
                    "key": "engagementType",
                    "displayName": "Engagement Type",
                    "hidden": False,
                    "options": [
                        {"id": f"0f6b3b3e-1f0a-4d1c-8d3e-3a1f6a9b01{i:02d}", "key": name}
                        for i, name in enumerate(ENGAGEMENT_TYPES)
                    ],
                },
                {
                    "id": "0f6b3b3e-1f0a-4d1c-8d3e-3a1f6a9b0003",
                    "type": "float",
                    "key": "fiscalYear",
                    "displayName": "Fiscal Year",
                    "hidden": False,
                },
            ],
        }
        ctx.store.collection(db.TEMPLATES).put(template)
        self.seed_classification(scope)
        for doc in self.world.documents:
            if not doc.client_id:
                continue
            client = self.world.client(doc.client_id)
            engagement = self.world.engagement(doc.engagement_id) if doc.engagement_id else None
            service = engagement.service if engagement and engagement.service in ENGAGEMENT_TYPES else "Other"
            fiscal = engagement.end.year if engagement else doc.created.year
            for token in doc.folder.split("/"):
                if token.startswith("FY") and token[2:6].isdigit():
                    fiscal = int(token[2:6])
            file_id = self.file_of_doc[doc.id]
            ctx.store.collection(db.METADATA).put(
                {
                    "id": f"{stable_hash('md', doc.id, length=8)}-0000-4000-8000-{stable_hash('md2', doc.id, length=12)}",
                    "item_type": "file",
                    "item_id": file_id,
                    "scope": scope,
                    "template": "clientInfo",
                    "values": {"clientName": client.name, "engagementType": service, "fiscalYear": fiscal},
                    "version": 0,
                    "typeVersion": 0,
                    "created_at": self.stamp(doc.created),
                    "can_edit": True,
                }
            )

    def seed_classification(self, scope: str) -> None:
        """Box's built-in security classification template, applied by data sensitivity."""
        ctx = self.ctx
        levels = {
            "Public": ("Data that can be shared outside the firm.", 2),
            "Internal": ("Firm-internal working material.", 3),
            "Confidential": ("Restricted to named people; client or partner confidential.", 5),
        }
        ctx.store.collection(db.TEMPLATES).put(
            {
                "id": "8c1d7a4e-2f0b-4b53-9d6e-5a1c7e9f0b21",
                "type": "metadata_template",
                "templateKey": metadata.CLASSIFICATION_KEY,
                "scope": scope,
                "displayName": "Classification",
                "hidden": False,
                "copyInstanceOnItemCopy": True,
                "fields": [
                    {
                        "id": "a3f2c1d0-7b6e-4a58-8c9d-0e1f2a3b4c5d",
                        "type": "enum",
                        "key": metadata.CLASSIFICATION_FIELD,
                        "displayName": "Classification",
                        "hidden": False,
                        "options": [
                            {
                                "id": stable_guid("box-classification", name),
                                "key": name,
                                "staticConfig": {
                                    "classification": {"classificationDefinition": text, "colorID": color}
                                },
                            }
                            for name, (text, color) in levels.items()
                        ],
                    }
                ],
            }
        )
        by_space = {
            "leadership": "Confidential",
            "finance": "Internal",
            "people": "Internal",
            "it": "Internal",
            "knowledge": "Public",
        }
        for doc in self.world.documents:
            label = "Confidential" if doc.restricted else by_space.get(doc.space_id)
            if label is None:
                continue
            ctx.store.collection(db.METADATA).put(
                {
                    "id": f"{stable_hash('cls', doc.id, length=8)}-0000-4000-8000-{stable_hash('cls2', doc.id, length=12)}",
                    "item_type": "file",
                    "item_id": self.file_of_doc[doc.id],
                    "scope": scope,
                    "template": metadata.CLASSIFICATION_KEY,
                    "values": {metadata.CLASSIFICATION_FIELD: label},
                    "version": 0,
                    "typeVersion": 0,
                    "created_at": self.stamp(doc.created),
                    "can_edit": True,
                }
            )

    # -- comments and tasks -------------------------------------------------------------
    def seed_social(self) -> None:
        ctx = self.ctx
        comments = ctx.store.collection(db.COMMENTS)
        tasks = ctx.store.collection(db.TASKS)
        assignments = ctx.store.collection(db.ASSIGNMENTS)
        notes = [
            "Can you tie this out to the trial balance before it goes to the client?",
            "Reviewed - a few formatting comments, otherwise good to go.",
            "Please confirm the prior-year comparatives with the signed copy.",
            "Updated per the partner's feedback; ready for another look.",
        ]
        count = 0
        for doc in self.world.documents:
            if not doc.engagement_id or not (
                doc.name.startswith(("Draft", "Management Letter", "Trial Balance"))
            ):
                continue
            engagement = self.world.engagement(doc.engagement_id)
            file_id = self.file_of_doc[doc.id]
            author = self.uid[engagement.lead_id]
            at = doc.modified + timedelta(hours=2)
            cid = self.number("comment", doc.id, 100_000_000_000, 999_999_999_999)
            comments.put(
                {
                    "type": "comment",
                    "id": cid,
                    "item_type": "file",
                    "item_id": file_id,
                    "message": notes[count % len(notes)],
                    "created_by": author,
                    "created_at": self.stamp(at),
                    "modified_at": self.stamp(at),
                    "is_reply_comment": False,
                }
            )
            count += 1
            if count % 3 == 0 and engagement.team_ids:
                assignee = self.uid[engagement.team_ids[0]]
                tid = self.number("task", doc.id, 10_000_000_000, 99_999_999_999)
                tasks.put(
                    {
                        "type": "task",
                        "id": tid,
                        "item_id": file_id,
                        "action": "review",
                        "message": f"Review {doc.name} before release",
                        "due_at": self.stamp(doc.modified + timedelta(days=5)),
                        "completion_rule": "all_assignees",
                        "created_by": author,
                        "created_at": self.stamp(at),
                        "is_completed": False,
                    }
                )
                aid = self.number("task_assignment", doc.id, 10_000_000_000, 99_999_999_999)
                assignments.put(
                    {
                        "type": "task_assignment",
                        "id": aid,
                        "task_id": tid,
                        "item_id": file_id,
                        "assigned_to": assignee,
                        "assigned_by": author,
                        "assigned_at": self.stamp(at),
                        "message": "",
                        "completed_at": None,
                        "reminded_at": None,
                        "resolution_state": "incomplete",
                    }
                )
            if count >= 24:
                break

    # -- tokens ------------------------------------------------------------------------
    def seed_dev_tokens(self) -> None:
        """Long-lived bearer tokens (``box-dev-<person id>``) so tests need no OAuth dance."""
        tokens = self.ctx.tokens
        for pid, uid in self.uid.items():
            tokens.issue_access(
                client_id="box-developer-console", user_id=uid, ttl=None, token=f"{DEV_TOKEN_PREFIX}{pid}",
                extra={"restricted_to": []},
            )  # fmt: skip
        tokens.issue_access(
            client_id="box-developer-console", user_id=self.service["id"], ttl=None,
            token=f"{DEV_TOKEN_PREFIX}enterprise", extra={"restricted_to": []},
        )  # fmt: skip
        tokens.register_client(CLIENT_ID, "box-client-secret", name="Halcyon Box App")

    # -- events ------------------------------------------------------------------------
    def seed_events(self) -> None:
        """A believable audit trail: creations, uploads, shares and invitations, oldest first."""
        ctx = self.ctx
        pending: list[tuple[str, str, str, Record, Record | None]] = []
        for folder in db.folders(ctx):
            if not db.is_root(folder):
                pending.append((folder["created_at"], "ITEM_CREATE", folder["created_by"], folder, None))
        for record in db.files(ctx):
            pending.append((record["created_at"], "ITEM_UPLOAD", record["created_by"], record, None))
            if record["_seq"] > 0:
                pending.append((record["modified_at"], "ITEM_UPLOAD", record["modified_by"], record, None))
            link = record.get("shared_link")
            if link:
                pending.append((link["created_at"], "ITEM_SHARED_CREATE", link["created_by"], record, None))
        for collab in db.collabs(ctx):
            item = db.item(ctx, collab["item_type"], collab["item_id"])
            if item is not None:
                kind = (
                    "COLLAB_ADD_COLLABORATOR"
                    if collab["status"] == "accepted"
                    else "COLLAB_INVITE_COLLABORATOR"
                )
                pending.append((collab["created_at"], kind, collab["created_by"], item, collab))
        pending.sort(key=lambda row: (row[0], row[1], row[3]["id"]))
        for index, (created_at, event_type, actor, record, collab) in enumerate(pending):
            lazy = ("collaboration", collab["id"]) if collab else (record["type"], record["id"])
            events.append_event(
                ctx, event_type, created_by=actor, source=None, item=record, created_at=created_at,
                event_id=stable_guid(self.seed_key, "box-event", index),
                session_id=stable_hash("box-session", index, length=12),
                lazy=lazy,
            )  # fmt: skip
        # Recent items: last 45 days of uploads/modifications per acting user.
        recents = ctx.store.collection(db.RECENTS)
        horizon = self.stamp(self.anchor - timedelta(days=45))
        for record in db.files(ctx):
            if record["modified_at"] >= horizon:
                rid = f"rc{len(recents) + 1}"
                recents.put(
                    {
                        "id": rid,
                        "user_id": record["modified_by"],
                        "item_type": "file",
                        "item_id": record["id"],
                        "interaction_type": "item_upload" if record["_seq"] == 0 else "item_modify",
                        "interacted_at": record["modified_at"],
                    }
                )


def seed(ctx: ProviderContext) -> None:
    Seeder(ctx).run()
