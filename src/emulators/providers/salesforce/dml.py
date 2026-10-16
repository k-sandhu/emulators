"""Insert / update / delete with Salesforce's validation, sharing checks, undo log and change log.

Every REST request that writes gets a :class:`Txn`: the undo log makes ``allOrNone`` composite
requests, tree inserts and collection saves atomic, and the event log feeds outbound messages
after commit. Records are mutated in place in the store (the undo log keeps deep copies).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from emulators.providers.salesforce import hooks
from emulators.providers.salesforce.coerce import coerce
from emulators.providers.salesforce.common import (
    SfError,
    insufficient_access,
    norm_id,
    not_found,
)
from emulators.providers.salesforce.org import Org, Record
from emulators.providers.salesforce.schema import SCHEMA, Field, SObject
from emulators.providers.salesforce.seedlib import blank
from emulators.providers.salesforce.sharing import Access


@dataclass
class Txn:
    """Undo + event log for one request."""

    undo: list[tuple[str, str, Record | None]] = field(default_factory=list)
    events: list[tuple[str, str, str]] = field(default_factory=list)
    org: Org | None = None

    def record(self, obj_name: str, rec: Record | None, *, created_id: str | None = None) -> None:
        """Remember the pre-change state of ``rec`` (or that ``created_id`` did not exist)."""
        if rec is None and created_id:
            self.undo.append((obj_name, created_id, None))
        elif rec is not None:
            self.undo.append((obj_name, rec["Id"], copy.deepcopy(rec)))

    def savepoint(self) -> tuple[int, int]:
        return len(self.undo), len(self.events)

    def rollback(self, to: tuple[int, int] = (0, 0)) -> None:
        del self.events[to[1] :]
        if len(self.undo) <= to[0]:
            return
        assert self.org is not None
        while len(self.undo) > to[0]:
            obj_name, rid, before = self.undo.pop()
            coll = self.org.coll(obj_name)
            if before is None:
                coll.delete(rid)
            else:
                coll.put_as(rid, before)


class Dml:
    def __init__(self, org: Org, access: Access, txn: Txn, *, system: bool = False) -> None:
        self.org = org
        self.access = access
        self.txn = txn
        self.txn.org = org
        self.system = system  # skip sharing/permission checks (seed-like internal writes)
        self.fiscal_start = int(
            (org.get("Organization", org.org_id18) or {}).get("FiscalYearStartMonth") or 1
        )

    # -- lookups -----------------------------------------------------------------------
    def obj(self, name: str) -> SObject:
        obj = SCHEMA.get(name)
        if obj is None:
            raise not_found()
        return obj

    def _require(self, obj: SObject, op: str) -> None:
        if self.system:
            return
        allowed = {"c": obj.createable, "u": obj.updateable, "d": obj.deletable}[op]
        verb = {"c": "inserted", "u": "updated", "d": "deleted"}[op]
        if not allowed or op not in self.access.perms(obj):
            raise SfError(403, "INSUFFICIENT_ACCESS_OR_READONLY", f"entity type cannot be {verb}: {obj.name}")

    def load(self, obj: SObject, record_id: Any, *, allow_deleted: bool = False) -> Record:
        """The record, or 404 NOT_FOUND when missing, invisible or malformed (ENTITY_IS_DELETED if deleted)."""
        rid = norm_id(record_id)
        rec = self.org.coll(obj.name).get(rid) if rid else None
        if rec is None or rec.get("_Purged"):
            raise not_found()
        if not self.system and not self.access.can_read(obj, rec):
            raise not_found()
        if rec.get("IsDeleted") and not allow_deleted:
            raise SfError(404, "ENTITY_IS_DELETED", "entity is deleted")
        return rec

    # -- field validation ---------------------------------------------------------------
    def resolve_lookup(
        self, obj: SObject, rel_name: str, ext_field: str, value: Any
    ) -> tuple[str, str | None]:
        """``Account: {QuickBooks_Customer_Id__c: "1"}`` -> (``AccountId``, the matching record id)."""
        f = obj.rel(rel_name)
        if f is None:
            raise SfError(
                400, "INVALID_FIELD", f"No such relationship '{rel_name}' on sobject of type {obj.name}"
            )
        if value is None:
            return f.name, None
        wanted = str(value).casefold()
        usable = False
        matches: list[str] = []
        for target_name in f.reference_to:
            target = SCHEMA.objects[target_name]
            tf = target.field(ext_field)
            if tf is None or not (tf.name == "Id" or tf.external_id or tf.id_lookup):
                continue
            usable = True
            for rec in self.org.coll(target.name):
                if (
                    not rec.get("IsDeleted")
                    and str(rec.get(tf.name) if tf.name != "Id" else rec["Id"]).casefold() == wanted
                    and (self.system or self.access.can_read(target, rec))
                ):
                    matches.append(rec["Id"])
        if not usable:
            raise SfError(
                400,
                "INVALID_FIELD",
                f"Provided external ID field does not exist or is not accessible: {ext_field}",
                [f.name],
            )
        if len(matches) > 1:
            raise SfError(
                400,
                "DUPLICATE_EXTERNAL_ID",
                f"Foreign key external ID: {value} matches more than one record for field {ext_field}",
                [f.name],
            )
        if matches:
            return f.name, matches[0]
        raise SfError(
            400,
            "INVALID_FIELD",
            f"Foreign key external ID: {value} not found for field {ext_field} in entity {f.reference_to[0]}",
            [f.name],
        )

    def expand_lookups(self, obj: SObject, data: dict[str, Any]) -> dict[str, Any]:
        """Replace relationship objects (``"Account": {"Ext__c": "x"}``) by the reference field they resolve to."""
        out: dict[str, Any] = {}
        for key, value in data.items():
            f = obj.rel(key) if isinstance(value, dict) and key != "attributes" else None
            if f is None:
                out[key] = value
                continue
            ext = {k: v for k, v in value.items() if k != "attributes"}
            if len(ext) != 1:
                raise SfError(
                    400, "INVALID_FIELD", f"Relationship '{key}' needs exactly one external id field"
                )
            ((ext_field, ext_value),) = ext.items()
            field_name, record_id = self.resolve_lookup(obj, key, ext_field, ext_value)
            if field_name in data or field_name in out:
                raise SfError(
                    400,
                    "INVALID_FIELD",
                    f"Cannot specify both '{field_name}' and the '{key}' relationship in the same request",
                    [field_name],
                )
            out[field_name] = record_id
        return out

    def _check_names(self, obj: SObject, data: dict[str, Any], *, update: bool) -> dict[str, Field]:
        chosen: dict[str, Field] = {}
        readonly: list[str] = []
        for key in data:
            if key == "attributes":
                continue
            f = obj.field(key)
            if f is None:
                raise SfError(400, "INVALID_FIELD", f"No such column '{key}' on sobject of type {obj.name}")
            writable = f.updateable if update else f.createable
            if not writable or f.type == "address":
                readonly.append(f.name)
            chosen[key] = f
        if readonly:
            raise SfError(
                400,
                "INVALID_FIELD_FOR_INSERT_UPDATE",
                f"Unable to create/update fields: {', '.join(readonly)}. Please check the security settings of "
                "this field and verify that it is read-only for your profile or permission set.",
                readonly,
            )
        return chosen

    def _check_required(self, obj: SObject, rec: Record) -> None:
        missing = [
            f.name for f in obj.fields if f.required and (rec.get(f.name) is None or rec.get(f.name) == "")
        ]
        if missing:
            raise SfError(
                400,
                "REQUIRED_FIELD_MISSING",
                f"Required fields are missing: [{', '.join(missing)}]",
                missing,
            )

    def _check_references(self, obj: SObject, rec: Record, names: set[str]) -> None:
        for name in names:
            f = obj.field(name)
            if f is None or not f.is_ref or not rec.get(f.name):
                continue
            value = rec[f.name]
            rid = norm_id(value)
            found = self.org.find(rid) if rid else None
            if rid is None or (
                found is None
                and rid[:3] not in {SCHEMA.objects[t].prefix for t in f.reference_to if t in SCHEMA.objects}
            ):
                raise SfError(
                    400,
                    "FIELD_INTEGRITY_EXCEPTION",
                    f"{f.label}: id value of incorrect type: {value}",
                    [f.name],
                )
            if found is None or found[1].get("IsDeleted"):
                raise SfError(400, "INVALID_CROSS_REFERENCE_KEY", "invalid cross reference id", [f.name])
            if found[0].name not in f.reference_to:
                raise SfError(
                    400,
                    "FIELD_INTEGRITY_EXCEPTION",
                    f"{f.label}: id value of incorrect type: {value}",
                    [f.name],
                )
            if not self.system and not self.access.can_read(*found):
                raise SfError(
                    400,
                    "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY",
                    f"insufficient access rights on cross-reference id: {rid}",
                    [f.name],
                )
            rec[f.name] = rid
        self._check_cycles(obj, rec, names)
        owner = rec.get("OwnerId") if "OwnerId" in names else None
        if owner:
            user = self.org.user(owner)
            if user is None:
                raise SfError(400, "INVALID_CROSS_REFERENCE_KEY", "invalid cross reference id", ["OwnerId"])
            if not user.get("IsActive"):
                raise SfError(400, "INACTIVE_OWNER_OR_USER", "owner cannot be an inactive user", ["OwnerId"])

    def _check_cycles(self, obj: SObject, rec: Record, names: set[str]) -> None:
        """A self-referencing hierarchy (parent account, role, campaign...) may not loop back on the record."""
        if not rec.get("Id"):
            return
        for name in names:
            f = obj.field(name)
            if f is None or not f.is_ref or f.reference_to != (obj.name,) or not rec.get(f.name):
                continue
            seen: set[str] = set()
            cur: str | None = rec[f.name]
            while cur and cur not in seen:
                if cur == rec["Id"]:
                    raise SfError(
                        400, "CIRCULAR_DEPENDENCY", f"{f.label}: cannot create a circular reference", [f.name]
                    )
                seen.add(cur)
                parent = self.org.get(obj.name, cur)
                cur = parent.get(f.name) if parent else None

    def _check_unique(self, obj: SObject, rec: Record) -> None:
        for f in obj.fields:
            if not f.unique or rec.get(f.name) in (None, ""):
                continue
            value = rec[f.name]
            for other in self.org.coll(obj.name):
                if other["Id"] == rec.get("Id") or other.get("_Purged"):
                    continue
                theirs = other.get(f.name)
                if theirs is not None and str(theirs).casefold() == str(value).casefold():
                    raise SfError(
                        400,
                        "DUPLICATE_VALUE",
                        f"duplicate value found: {f.name} duplicates value on record with id: {other['Id']}",
                        [],
                    )

    def _keep_blob(self, rec: Record, f: Field, value: Any) -> Any:
        """Binary fields live in the content-addressed blob store; the record keeps a ``blob:`` handle."""
        if not isinstance(value, bytes):
            return value
        rec[f"_size_{f.name}"] = len(value)
        return "blob:" + self.org.store.blobs.put(value)

    # -- insert --------------------------------------------------------------------------
    def insert(self, obj_name: str, data: dict[str, Any]) -> Record:
        obj = self.obj(obj_name)
        self._require(obj, "c")
        data = self.expand_lookups(obj, data)
        chosen = self._check_names(obj, data, update=False)
        rec = blank(obj)
        for f in obj.fields:
            if f.type == "boolean" and f.default:
                rec[f.name] = True
            if f.type == "picklist" and f.default_pick:
                rec[f.name] = f.default_pick
        provided: set[str] = set()
        for key, f in chosen.items():
            rec[f.name] = self._keep_blob(rec, f, coerce(f, data[key]))
            provided.add(f.name)
        if obj.has_owner and not rec.get("OwnerId"):
            rec["OwnerId"] = self.access.user["Id"]
            provided.add("OwnerId")
        self._check_required(obj, rec)
        self._check_references(obj, rec, provided)
        hooks.before_save(self, obj, rec, None, provided)
        self._check_unique(obj, rec)
        hooks.validate(self, obj, rec, None)
        now = self.org.now_str()
        rec["Id"] = self.org.new_id(obj.name)
        user_id = self.access.user["Id"]
        for key, value in (
            ("IsDeleted", False),
            ("CreatedDate", now),
            ("CreatedById", user_id),
            ("LastModifiedDate", now),
            ("LastModifiedById", user_id),
            ("SystemModstamp", now),
        ):
            if key in rec:
                rec[key] = value
        self.txn.record(obj.name, None, created_id=rec["Id"])
        self.org.put(obj.name, rec)
        hooks.after_save(self, obj, rec, None)
        self.txn.events.append(("create", obj.name, rec["Id"]))
        return rec

    # -- update --------------------------------------------------------------------------
    def update(self, obj_name: str, record_id: Any, data: dict[str, Any]) -> Record:
        obj = self.obj(obj_name)
        self._require(obj, "u")
        rec = self.load(obj, record_id)
        if not self.system and not self.access.can_edit(obj, rec):
            raise insufficient_access()
        data = self.expand_lookups(obj, data)
        chosen = self._check_names(obj, data, update=True)
        before = copy.deepcopy(rec)
        updated = copy.deepcopy(rec)
        provided: set[str] = set()
        for key, f in chosen.items():
            updated[f.name] = self._keep_blob(updated, f, coerce(f, data[key]))
            provided.add(f.name)
        self._check_required(obj, updated)
        self._check_references(obj, updated, provided)
        hooks.before_save(self, obj, updated, before, provided)
        self._check_unique(obj, updated)
        hooks.validate(self, obj, updated, before)
        now = self.org.now_str()
        updated["LastModifiedDate"] = updated["SystemModstamp"] = now
        updated["LastModifiedById"] = self.access.user["Id"]
        self.txn.undo.append((obj.name, rec["Id"], before))
        rec.clear()
        rec.update(updated)
        hooks.after_save(self, obj, rec, before)
        self.txn.events.append(("update", obj.name, rec["Id"]))
        return rec

    # -- delete --------------------------------------------------------------------------
    def delete(self, obj_name: str, record_id: Any) -> None:
        obj = self.obj(obj_name)
        self._require(obj, "d")
        rec = self.load(obj, record_id)
        if not self.system and not self.access.can_delete(obj, rec):
            raise insufficient_access()
        self._check_restricted(obj, rec)
        self._delete_record(obj, rec)

    def _check_restricted(self, obj: SObject, rec: Record) -> None:
        """Refuse deletes that would orphan a restricted-delete reference (anywhere down the cascade)."""
        if obj.name == "Pricebook2" and rec.get("IsStandard"):
            raise SfError(400, "DELETE_FAILED", "Cannot delete the standard price book.", [])
        for rel in SCHEMA.children(obj.name):
            child_obj = SCHEMA.objects[rel.child]
            field = child_obj.field(rel.field)
            if field is None or field.name == "OwnerId" or not (field.restricted_delete or field.cascade):
                continue
            kids = [
                c
                for c in self.org.coll(child_obj.name)
                if c.get(rel.field) == rec["Id"] and not c.get("IsDeleted")
            ]
            if kids and field.restricted_delete:
                raise SfError(
                    400,
                    "DELETE_FAILED",
                    f"Cannot delete {obj.label.lower()}: it is referenced by {len(kids)} {child_obj.plural.lower()}.",
                    [],
                )
            if field.cascade:
                for kid in kids:
                    self._check_restricted(child_obj, kid)

    def _delete_record(self, obj: SObject, rec: Record) -> None:
        if rec.get("IsDeleted"):
            return
        now = self.org.now_str()
        self.txn.record(obj.name, rec)
        rec["IsDeleted"] = True
        rec["_DeletedDate"] = now
        rec["SystemModstamp"] = now
        self.txn.events.append(("delete", obj.name, rec["Id"]))
        for activity in ("Task", "Event"):
            if obj.name == activity:
                continue
            for act in list(self.org.coll(activity)):
                if not act.get("IsDeleted") and rec["Id"] in (act.get("WhatId"), act.get("WhoId")):
                    # activities live and die with the record they are attached to
                    self._delete_record(SCHEMA.objects[activity], act)
        for rel in SCHEMA.children(obj.name):
            child_obj = SCHEMA.objects[rel.child]
            child_field = child_obj.field(rel.field)
            for child in list(self.org.coll(child_obj.name)):
                if child.get(rel.field) != rec["Id"] or child.get("IsDeleted"):
                    continue
                if child_field is not None and child_field.cascade:
                    self._delete_record(child_obj, child)
                else:
                    self.txn.record(child_obj.name, child)
                    child[rel.field] = None
        hooks.after_delete(self, obj, rec)

    def undelete(self, obj_name: str, record_id: Any) -> Record:
        obj = self.obj(obj_name)
        rid = norm_id(record_id)
        rec = self.org.coll(obj.name).get(rid) if rid else None
        if rec is None or rec.get("_Purged") or not rec.get("IsDeleted"):
            raise not_found()
        self.txn.record(obj.name, rec)
        rec["IsDeleted"] = False
        rec.pop("_DeletedDate", None)
        rec["SystemModstamp"] = self.org.now_str()
        self.txn.events.append(("undelete", obj.name, rec["Id"]))
        return rec

    def purge(self, obj_name: str, record_id: Any) -> None:
        """Hard delete (Bulk ``hardDelete``, emptying the recycle bin)."""
        obj = self.obj(obj_name)
        rec = self.load(obj, record_id, allow_deleted=True)
        if not rec.get("IsDeleted"):
            self._delete_record(obj, rec)
        self.txn.record(obj.name, rec)
        rec["_Purged"] = True

    # -- upsert --------------------------------------------------------------------------
    def find_by_external(self, obj: SObject, field_name: str, value: Any) -> list[Record]:
        f = obj.field(field_name)
        if f is None:
            raise SfError(
                404,
                "NOT_FOUND",
                f"Provided external ID field does not exist or is not accessible: {field_name}",
            )
        if f.name == "Id":
            rid = norm_id(value)
            rec = self.org.coll(obj.name).get(rid) if rid else None
            return (
                [rec]
                if (
                    rec
                    and not rec.get("IsDeleted")
                    and not rec.get("_Purged")
                    and (self.system or self.access.can_read(obj, rec))
                )
                else []
            )
        if not (f.external_id or f.unique or f.id_lookup):
            raise SfError(
                404,
                "NOT_FOUND",
                f"Provided external ID field does not exist or is not accessible: {field_name}",
            )
        wanted = str(value).casefold()
        return [
            r
            for r in self.org.coll(obj.name)
            if not r.get("IsDeleted")
            and not r.get("_Purged")
            and r.get(f.name) is not None
            and str(r.get(f.name)).casefold() == wanted
            and (self.system or self.access.can_read(obj, r))
        ]
