"""The sharing model and object permissions, enforced for every delegated token.

Org-wide defaults (see ``Organization.Default*Access``):

=========================================  =================================================
Account, Contact, Campaign                 Public Read Only - everyone reads; edit = owner,
                                           users above the owner in the role hierarchy, admins
Opportunity, Lead, Case, Engagement__c     Private - read and edit = owner, role ancestors, admins
OpportunityContactRole/LineItem            controlled by the parent Opportunity
CampaignMember                             controlled by the parent Campaign
Task, Event                                owner / role ancestors, or access to the related record
Product2, Pricebook2, PricebookEntry,      readable by everyone; only admins write
User, UserRole, Profile, Group ...
=========================================  =================================================

"Admins" are profiles with *View All Data* / *Modify All Data* (System Administrator and the
API-only integration profile). Object-level create/read/edit/delete comes from the profile
name (``Read Only`` can only read).
"""

from __future__ import annotations

from typing import Any

from emulators.providers.salesforce.org import Org, Record
from emulators.providers.salesforce.schema import SObject

OWD: dict[str, str] = {
    "Account": "Read",
    "Contact": "Read",
    "Campaign": "Read",
    "Opportunity": "None",
    "Lead": "None",
    "Case": "None",
    "Engagement__c": "None",
}
PUBLIC_OBJECTS = {
    "Product2",
    "Pricebook2",
    "PricebookEntry",
    "User",
    "UserRole",
    "Profile",
    "Group",
    "RecordType",
    "Organization",
    "OpportunityStage",
    "LeadStatus",
    "CaseStatus",
}
SETUP_OBJECTS = {
    "User",
    "UserRole",
    "Profile",
    "Group",
    "RecordType",
    "Organization",
    "OpportunityStage",
    "LeadStatus",
    "CaseStatus",
}
PARENT_OF: dict[str, tuple[str, str]] = {
    "OpportunityContactRole": ("Opportunity", "OpportunityId"),
    "OpportunityLineItem": ("Opportunity", "OpportunityId"),
    "CampaignMember": ("Campaign", "CampaignId"),
}
SALES_OBJECTS = {
    "Account",
    "Contact",
    "Opportunity",
    "OpportunityContactRole",
    "OpportunityLineItem",
    "Lead",
    "Case",
    "Task",
    "Event",
    "Engagement__c",
    "ContentDocument",
    "ContentVersion",
    "ContentDocumentLink",
    "Attachment",
}
FILE_OBJECTS = {"ContentDocument", "ContentVersion", "ContentDocumentLink", "Attachment"}


def object_perms(profile_name: str, modify_all: bool, obj: SObject) -> str:
    """Subset of ``crud`` the profile has on ``obj``."""
    allowed = ""
    allowed += "c" if obj.createable else ""
    allowed += "r" if obj.queryable else ""
    allowed += "u" if obj.updateable else ""
    allowed += "d" if obj.deletable else ""
    if modify_all:
        return allowed
    if obj.name in SETUP_OBJECTS:
        return "r"
    if profile_name == "Read Only":
        return "r"
    if obj.name in SALES_OBJECTS:
        return allowed
    if profile_name == "Sales User" and obj.name in ("Campaign", "CampaignMember"):
        return "".join(ch for ch in allowed if ch in "crud")
    if obj.name == "Campaign":
        return "r"
    if obj.name == "CampaignMember":
        return "r"
    return "r"


class Access:
    """Everything the sharing rules need to know about one calling user."""

    def __init__(self, org: Org, user: Record) -> None:
        self.org = org
        self.user = user
        profile = org.get("Profile", user.get("ProfileId")) or {}
        self.profile_name: str = profile.get("Name", "")
        self.modify_all = bool(profile.get("PermissionsModifyAllData"))
        self.view_all = self.modify_all or bool(profile.get("PermissionsViewAllData"))
        self.is_admin = self.modify_all
        self.owner_ids = self._visible_owner_ids()
        self._doc_links: dict[str, list[str]] | None = None

    # -- hierarchy ---------------------------------------------------------------------
    def _visible_owner_ids(self) -> set[str]:
        """The user plus everyone strictly below them in the role hierarchy."""
        me = {str(self.user["Id"])}
        my_role = self.user.get("UserRoleId")
        if not my_role:
            return me
        parent_of = {r["Id"]: r.get("ParentRoleId") for r in self.org.coll("UserRole")}

        def below(role_id: str | None) -> bool:
            seen = 0
            while role_id and seen < 64:
                role_id = parent_of.get(role_id)
                if role_id == my_role:
                    return True
                seen += 1
            return False

        for u in self.org.coll("User"):
            if u.get("UserRoleId") and below(u["UserRoleId"]):
                me.add(u["Id"])
        return me

    # -- object permissions ------------------------------------------------------------
    def perms(self, obj: SObject) -> str:
        return object_perms(self.profile_name, self.modify_all, obj)

    def can_create_object(self, obj: SObject) -> bool:
        return "c" in self.perms(obj)

    def can_update_object(self, obj: SObject) -> bool:
        return "u" in self.perms(obj)

    def can_delete_object(self, obj: SObject) -> bool:
        return "d" in self.perms(obj)

    # -- files ---------------------------------------------------------------------------
    def _links(self) -> dict[str, list[str]]:
        """ContentDocument id -> ids of the records it is shared with (built once per request)."""
        if self._doc_links is None:
            index: dict[str, list[str]] = {}
            for link in self.org.coll("ContentDocumentLink"):
                if not link.get("IsDeleted"):
                    index.setdefault(link["ContentDocumentId"], []).append(link["LinkedEntityId"])
            self._doc_links = index
        return self._doc_links

    def _entity_readable(self, entity_id: str) -> bool:
        if entity_id.startswith("005"):  # a personal library is visible to its owner and the owner's managers
            return entity_id in self.owner_ids
        found = self.org.find(entity_id)
        return found is not None and not found[1].get("IsDeleted") and self.can_read(*found)

    def _doc_readable(self, doc: Record | None) -> bool:
        if doc is None:
            return False
        if doc.get("OwnerId") in self.owner_ids:
            return True
        return any(self._entity_readable(e) for e in self._links().get(doc["Id"], ()))

    def _doc_editable(self, doc: Record | None) -> bool:
        return doc is not None and (self.modify_all or doc.get("OwnerId") in self.owner_ids)

    def _doc_of(self, obj: SObject, rec: Record) -> Record | None:
        """The ContentDocument a file record belongs to."""
        if obj.name == "ContentDocument":
            return rec
        return self.org.get("ContentDocument", rec.get("ContentDocumentId"))

    def _file_read(self, obj: SObject, rec: Record) -> bool:
        if obj.name == "Attachment":
            parent = self.org.find(rec.get("ParentId")) if rec.get("ParentId") else None
            return parent is None or self.can_read(*parent)
        if obj.name == "ContentDocumentLink":
            return self._entity_readable(rec["LinkedEntityId"]) or self._doc_editable(self._doc_of(obj, rec))
        return self._doc_readable(self._doc_of(obj, rec))

    def _file_edit(self, obj: SObject, rec: Record) -> bool:
        if obj.name == "Attachment":
            parent = self.org.find(rec.get("ParentId")) if rec.get("ParentId") else None
            return parent is not None and self.can_edit(*parent)
        if obj.name == "ContentDocumentLink":
            found = self.org.find(rec["LinkedEntityId"])
            return self._doc_editable(self._doc_of(obj, rec)) or (
                found is not None and not rec["LinkedEntityId"].startswith("005") and self.can_edit(*found)
            )
        return self._doc_editable(self._doc_of(obj, rec))

    # -- record visibility -------------------------------------------------------------
    def _owner_visible(self, rec: Record) -> bool:
        return rec.get("OwnerId") in self.owner_ids

    def _parent(self, obj: SObject, rec: Record) -> tuple[SObject, Record] | None:
        from emulators.providers.salesforce.schema import SCHEMA

        spec = PARENT_OF.get(obj.name)
        if spec is not None:
            parent_obj = SCHEMA.objects[spec[0]]
            parent = self.org.get(parent_obj.name, rec.get(spec[1]))
            return (parent_obj, parent) if parent else None
        return None

    def _activity_parents(self, rec: Record) -> list[tuple[SObject, Record]]:
        """The What and Who records a task/event hangs off (either one can grant access)."""
        found = [self.org.find(rec[key]) for key in ("WhatId", "WhoId") if rec.get(key)]
        return [f for f in found if f is not None]

    def can_read(self, obj: SObject, rec: Record) -> bool:
        if self.view_all or obj.name in PUBLIC_OBJECTS:
            return True
        if obj.name in FILE_OBJECTS:
            return self._file_read(obj, rec)
        if obj.name in PARENT_OF:
            parent = self._parent(obj, rec)
            return parent is None or self.can_read(*parent)
        if obj.name in ("Task", "Event"):
            return self._owner_visible(rec) or any(self.can_read(*p) for p in self._activity_parents(rec))
        if OWD.get(obj.name) in ("Read", "Edit"):
            return True
        return self._owner_visible(rec)

    def can_edit(self, obj: SObject, rec: Record) -> bool:
        if not self.can_update_object(obj):
            return False
        if self.modify_all:
            return True
        if obj.name in PUBLIC_OBJECTS:
            return False
        if obj.name in FILE_OBJECTS:
            return self._file_edit(obj, rec)
        if obj.name in PARENT_OF:
            parent = self._parent(obj, rec)
            return parent is not None and self.can_edit(*parent)
        if obj.name in ("Task", "Event"):
            return self._owner_visible(rec) or any(self.can_edit(*p) for p in self._activity_parents(rec))
        if OWD.get(obj.name) == "Edit":
            return True
        return self._owner_visible(rec)

    def can_delete(self, obj: SObject, rec: Record) -> bool:
        if not self.can_delete_object(obj):
            return False
        if self.modify_all:
            return True
        if obj.name in PUBLIC_OBJECTS:
            return False
        if obj.name in FILE_OBJECTS:
            return self._file_edit(obj, rec)
        if obj.name in PARENT_OF:
            parent = self._parent(obj, rec)
            return parent is not None and self.can_edit(*parent)
        if obj.name in ("Task", "Event"):
            return self._owner_visible(rec) or self.can_edit(obj, rec)
        return self._owner_visible(rec)

    def readable(self, obj: SObject) -> Any:
        """A predicate over records of ``obj``; cheap shortcuts for the common cases."""
        if self.view_all or obj.name in PUBLIC_OBJECTS:
            return lambda rec: True
        if obj.name not in PARENT_OF and obj.name not in ("Task", "Event") and obj.name not in FILE_OBJECTS:
            if OWD.get(obj.name) in ("Read", "Edit"):
                return lambda rec: True
            owners = self.owner_ids
            return lambda rec: rec.get("OwnerId") in owners
        return lambda rec: self.can_read(obj, rec)


def access_for(org: Org, user: Record) -> Access:
    """Per-request cache keyed by user id (users/roles rarely change inside one request)."""
    return Access(org, user)
