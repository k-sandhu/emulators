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
    "Product2", "Pricebook2", "PricebookEntry", "User", "UserRole", "Profile", "Group", "RecordType",
    "Organization",
}  # fmt: skip
SETUP_OBJECTS = {"User", "UserRole", "Profile", "Group", "RecordType", "Organization"}
PARENT_OF: dict[str, tuple[str, str]] = {
    "OpportunityContactRole": ("Opportunity", "OpportunityId"),
    "OpportunityLineItem": ("Opportunity", "OpportunityId"),
    "CampaignMember": ("Campaign", "CampaignId"),
}
SALES_OBJECTS = {
    "Account", "Contact", "Opportunity", "OpportunityContactRole", "OpportunityLineItem", "Lead", "Case",
    "Task", "Event", "Engagement__c",
}  # fmt: skip


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
        if obj.name in ("Task", "Event"):
            for key in ("WhatId", "WhoId"):
                found = self.org.find(rec.get(key)) if rec.get(key) else None
                if found:
                    return found
        return None

    def can_read(self, obj: SObject, rec: Record) -> bool:
        if self.view_all or obj.name in PUBLIC_OBJECTS:
            return True
        if obj.name in PARENT_OF:
            parent = self._parent(obj, rec)
            return parent is None or self.can_read(*parent)
        if obj.name in ("Task", "Event"):
            if self._owner_visible(rec):
                return True
            parent = self._parent(obj, rec)
            return parent is not None and self.can_read(*parent)
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
        if obj.name in PARENT_OF:
            parent = self._parent(obj, rec)
            return parent is not None and self.can_edit(*parent)
        if obj.name in ("Task", "Event"):
            if self._owner_visible(rec):
                return True
            parent = self._parent(obj, rec)
            return parent is not None and self.can_edit(*parent)
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
        if obj.name not in PARENT_OF and obj.name not in ("Task", "Event"):
            if OWD.get(obj.name) in ("Read", "Edit"):
                return lambda rec: True
            owners = self.owner_ids
            return lambda rec: rec.get("OwnerId") in owners
        return lambda rec: self.can_read(obj, rec)


def access_for(org: Org, user: Record) -> Access:
    """Per-request cache keyed by user id (users/roles rarely change inside one request)."""
    return Access(org, user)
