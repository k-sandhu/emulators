"""The sObject metadata model: fields, objects, relationships and the describe renderers.

Everything the REST ``describe`` calls return, and everything the SOQL engine needs to
validate field names and coerce literals, comes from these dataclasses. Object definitions
live in :mod:`schema_org` and :mod:`schema_crm` and are assembled into :data:`SCHEMA`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from emulators.providers.salesforce.common import MASTER_RECORD_TYPE_ID, camel_to_label

SOAP_TYPES = {
    "id": "tns:ID",
    "reference": "tns:ID",
    "boolean": "xsd:boolean",
    "currency": "xsd:double",
    "double": "xsd:double",
    "percent": "xsd:double",
    "int": "xsd:int",
    "date": "xsd:date",
    "datetime": "xsd:dateTime",
    "time": "xsd:time",
    "address": "urn:address",
    "location": "urn:location",
    "base64": "xsd:base64Binary",
}
STRING_TYPES = {"string", "textarea", "email", "phone", "url", "picklist", "multipicklist", "combobox"}
NUMERIC_TYPES = {"int", "double", "currency", "percent"}


@dataclass
class Field:
    name: str
    type: str
    label: str = ""
    length: int = 0
    precision: int = 0
    scale: int = 0
    nillable: bool = True
    createable: bool = True
    updateable: bool = True
    defaulted: bool = False
    default: Any = None
    unique: bool = False
    external_id: bool = False
    id_lookup: bool = False
    filterable: bool = True
    sortable: bool = True
    groupable: bool = True
    picklist: tuple[tuple[str, str], ...] = ()
    restricted: bool = False
    default_pick: str | None = None
    reference_to: tuple[str, ...] = ()
    relationship: str | None = None
    child: str | dict[str, str] | None = None
    cascade: bool = False
    restricted_delete: bool = False
    calculated: bool = False
    auto_number: bool = False
    custom: bool = False
    name_field: bool = False
    compound: str | None = None
    components: tuple[str, ...] = ()
    help: str | None = None
    extra: str | None = None
    searchable: bool = True

    def __post_init__(self) -> None:
        if not self.label:
            self.label = camel_to_label(self.name)
        self.custom = self.custom or self.name.endswith("__c")
        if self.type == "textarea" and self.length > 4000:
            self.filterable = False
            self.sortable = False
            self.groupable = False
        if self.type in ("address", "location", "base64"):
            self.filterable = self.sortable = self.groupable = False
        if self.type == "multipicklist":
            self.groupable = False

    # -- classification ----------------------------------------------------------------
    @property
    def is_string(self) -> bool:
        return self.type in STRING_TYPES

    @property
    def is_numeric(self) -> bool:
        return self.type in NUMERIC_TYPES

    @property
    def is_ref(self) -> bool:
        return self.type == "reference"

    @property
    def polymorphic(self) -> bool:
        return len(self.reference_to) > 1

    @property
    def required(self) -> bool:
        return not self.nillable and self.createable and not self.defaulted

    def picklist_values(self) -> list[str]:
        return [v for v, _ in self.picklist]

    # -- describe ----------------------------------------------------------------------
    def describe(self, *, can_create: bool = True, can_update: bool = True) -> dict[str, Any]:
        is_text = self.type in STRING_TYPES or self.type in ("id", "reference")
        length = self.length or (18 if self.type in ("id", "reference") else 0)
        return {
            "aggregatable": self.groupable or self.type in NUMERIC_TYPES,
            "aiPredictionField": False,
            "autoNumber": self.auto_number,
            "byteLength": length * 3 if self.type in STRING_TYPES else (length if is_text else 0),
            "calculated": self.calculated,
            "calculatedFormula": None,
            "cascadeDelete": self.cascade,
            "caseSensitive": False,
            "compoundFieldName": self.compound,
            "controllerName": None,
            "createable": self.createable and can_create,
            "custom": self.custom,
            "defaultValue": self.default
            if self.type == "boolean"
            else (self.default_pick if self.type == "picklist" else None),
            "defaultValueFormula": None,
            "defaultedOnCreate": self.defaulted,
            "dependentPicklist": False,
            "deprecatedAndHidden": False,
            "digits": 9 if self.type == "int" else 0,
            "displayLocationInDecimal": False,
            "encrypted": False,
            "externalId": self.external_id,
            "extraTypeInfo": self.extra,
            "filterable": self.filterable,
            "filteredLookupInfo": None,
            "formulaTreatNullNumberAsZero": False,
            "groupable": self.groupable,
            "highScaleNumber": False,
            "htmlFormatted": False,
            "idLookup": self.id_lookup or self.external_id or self.name == "Id",
            "inlineHelpText": self.help,
            "label": self.label,
            "length": length if self.type not in ("boolean", "date", "datetime", "int") else 0,
            "mask": None,
            "maskType": None,
            "name": self.name,
            "nameField": self.name_field,
            "namePointing": self.polymorphic,
            "nillable": self.nillable,
            "permissionable": self.name not in ("Id", "IsDeleted", "CreatedDate", "CreatedById")
            and not self.name_field
            and self.nillable,
            "picklistValues": [
                {
                    "active": True,
                    "defaultValue": value == self.default_pick,
                    "label": label,
                    "validFor": None,
                    "value": value,
                }
                for value, label in self.picklist
            ],
            "polymorphicForeignKey": self.polymorphic,
            "precision": self.precision if self.type in ("double", "currency", "percent") else 0,
            "queryByDistance": False,
            "referenceTargetField": None,
            "referenceTo": list(self.reference_to),
            "relationshipName": self.relationship,
            "relationshipOrder": None,
            "restrictedDelete": self.restricted_delete,
            "restrictedPicklist": self.restricted,
            "scale": self.scale if self.type in ("double", "currency", "percent") else 0,
            "searchPrefilterable": False,
            "soapType": SOAP_TYPES.get(self.type, "xsd:string"),
            "sortable": self.sortable,
            "type": self.type,
            "unique": self.unique,
            "updateable": self.updateable and can_update,
            "writeRequiresMasterRead": False,
        }


# -- builders --------------------------------------------------------------------------


def s(name: str, label: str = "", length: int = 255, **kw: Any) -> Field:
    return Field(name, "string", label, length=length, **kw)


def textarea(name: str, label: str = "", length: int = 32000, **kw: Any) -> Field:
    kw.setdefault("extra", "plaintextarea")
    return Field(name, "textarea", label, length=length, **kw)


def email(name: str, label: str = "", **kw: Any) -> Field:
    return Field(name, "email", label, length=80, **kw)


def phone(name: str, label: str = "", **kw: Any) -> Field:
    return Field(name, "phone", label, length=40, **kw)


def url(name: str, label: str = "", length: int = 255, **kw: Any) -> Field:
    return Field(name, "url", label, length=length, **kw)


def pick(
    name: str,
    values: Iterable[str],
    label: str = "",
    *,
    restricted: bool = False,
    default: str | None = None,
    length: int = 40,
    **kw: Any,
) -> Field:
    vals = tuple((v, v) for v in values)
    return Field(
        name, "picklist", label, length=length, picklist=vals, restricted=restricted, default_pick=default, **kw
    )


def multipick(name: str, values: Iterable[str], label: str = "", length: int = 4099, **kw: Any) -> Field:
    return Field(name, "multipicklist", label, length=length, picklist=tuple((v, v) for v in values), **kw)


def ref(
    name: str,
    targets: str | tuple[str, ...],
    relationship: str,
    label: str = "",
    *,
    child: str | dict[str, str] | None = None,
    **kw: Any,
) -> Field:
    tuple_targets = (targets,) if isinstance(targets, str) else tuple(targets)
    return Field(
        name,
        "reference",
        label,
        length=18,
        reference_to=tuple_targets,
        relationship=relationship,
        child=child,
        **kw,
    )


def flag(name: str, label: str = "", default: bool = False, **kw: Any) -> Field:
    return Field(name, "boolean", label, nillable=False, defaulted=True, default=default, **kw)


def currency(name: str, label: str = "", precision: int = 18, scale: int = 2, **kw: Any) -> Field:
    return Field(name, "currency", label, precision=precision, scale=scale, **kw)


def number(name: str, label: str = "", precision: int = 18, scale: int = 0, **kw: Any) -> Field:
    return Field(name, "double", label, precision=precision, scale=scale, **kw)


def percent(name: str, label: str = "", precision: int = 3, scale: int = 0, **kw: Any) -> Field:
    return Field(name, "percent", label, precision=precision, scale=scale, **kw)


def integer(name: str, label: str = "", **kw: Any) -> Field:
    return Field(name, "int", label, **kw)


def date_(name: str, label: str = "", **kw: Any) -> Field:
    return Field(name, "date", label, **kw)


def datetime_(name: str, label: str = "", **kw: Any) -> Field:
    return Field(name, "datetime", label, **kw)


def address(prefix: str, label: str, *, street: str = "Street", geo: bool = True) -> list[Field]:
    """Component fields + the compound ``<prefix>Address`` field (read-only, nested on the wire)."""
    comp = f"{prefix}Address"
    fields = [
        textarea(f"{prefix}{street}", f"{label} Street", 255, compound=comp, extra="plaintextarea"),
        s(f"{prefix}City", f"{label} City", 40, compound=comp),
        s(f"{prefix}State", f"{label} State/Province", 80, compound=comp),
        s(f"{prefix}PostalCode", f"{label} Zip/Postal Code", 20, compound=comp),
        s(f"{prefix}Country", f"{label} Country", 80, compound=comp),
    ]
    if geo:
        fields += [
            Field(f"{prefix}Latitude", "double", f"{label} Latitude", precision=18, scale=15, compound=comp),
            Field(f"{prefix}Longitude", "double", f"{label} Longitude", precision=18, scale=15, compound=comp),
            pick(f"{prefix}GeocodeAccuracy", ["Address", "NearAddress", "Block", "Street", "ExtendedZip",
                                             "Zip", "Neighborhood", "City", "County", "State", "Unknown"],
                 f"{label} Geocode Accuracy", restricted=True, compound=comp),
        ]
    fields.append(
        Field(
            comp,
            "address",
            f"{label} Address",
            createable=False,
            updateable=False,
            components=tuple(f.name for f in fields),
        )
    )
    return fields


# -- shared field groups -----------------------------------------------------------------


def id_field(label: str) -> Field:
    return Field(
        "Id",
        "id",
        label,
        length=18,
        nillable=False,
        createable=False,
        updateable=False,
        defaulted=True,
        id_lookup=True,
    )


def deleted_field() -> Field:
    return Field(
        "IsDeleted",
        "boolean",
        "Deleted",
        nillable=False,
        createable=False,
        updateable=False,
        defaulted=True,
        default=False,
    )


def owner_field(targets: tuple[str, ...] = ("User",), child: str | None = None, label: str = "Owner ID") -> Field:
    return ref(
        "OwnerId", targets, "Owner", label, nillable=False, defaulted=True, child=child, restricted_delete=True
    )


def audit_fields() -> list[Field]:
    ro: dict[str, Any] = {"nillable": False, "createable": False, "updateable": False, "defaulted": True}
    return [
        datetime_("CreatedDate", "Created Date", **ro),
        ref("CreatedById", "User", "CreatedBy", "Created By ID", **ro),
        datetime_("LastModifiedDate", "Last Modified Date", **ro),
        ref("LastModifiedById", "User", "LastModifiedBy", "Last Modified By ID", **ro),
        datetime_("SystemModstamp", "System Modstamp", **ro),
    ]


def viewed_fields() -> list[Field]:
    ro: dict[str, Any] = {"createable": False, "updateable": False}
    return [
        datetime_("LastViewedDate", "Last Viewed Date", **ro),
        datetime_("LastReferencedDate", "Last Referenced Date", **ro),
    ]


# -- objects ---------------------------------------------------------------------------


@dataclass
class ChildRel:
    parent: str
    name: str
    child: str
    field: str


@dataclass
class SObject:
    name: str
    label: str
    plural: str
    prefix: str
    fields: list[Field]
    custom: bool = False
    createable: bool = True
    updateable: bool = True
    deletable: bool = True
    queryable: bool = True
    searchable: bool = True
    feed: bool = False
    mergeable: bool = False
    has_activities: bool = False
    name_field: str = "Name"
    _by_name: dict[str, Field] = field(default_factory=dict, repr=False)
    _by_rel: dict[str, Field] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.custom = self.custom or self.name.endswith("__c")
        self._by_name = {f.name.lower(): f for f in self.fields}
        self._by_rel = {f.relationship.lower(): f for f in self.fields if f.relationship}

    def field(self, name: str) -> Field | None:
        return self._by_name.get(name.lower())

    def rel(self, name: str) -> Field | None:
        return self._by_rel.get(name.lower())

    @property
    def has_owner(self) -> bool:
        return "ownerid" in self._by_name

    @property
    def standard_fields(self) -> list[Field]:
        return [f for f in self.fields if not f.custom]

    @property
    def custom_fields(self) -> list[Field]:
        return [f for f in self.fields if f.custom]

    def key_prefix_or_none(self) -> str | None:
        return self.prefix or None

    # -- describe ----------------------------------------------------------------------
    def urls(self, version: str) -> dict[str, str]:
        base = f"/services/data/v{version}/sobjects/{self.name}"
        return {
            "sobject": base,
            "describe": f"{base}/describe",
            "rowTemplate": f"{base}/{{ID}}",
            "compactLayouts": f"{base}/describe/compactLayouts",
            "layouts": f"{base}/describe/layouts",
            "approvalLayouts": f"{base}/describe/approvalLayouts",
            "listviews": f"{base}/listviews",
            "quickActions": f"{base}/quickActions",
            "namedLayouts": f"{base}/describe/namedLayouts/{{name}}",
            "defaultValues": f"{base}/defaultValues?recordTypeId&fields",
            "uiDetailTemplate": f"https://halcyonadvisory.lightning.force.com/{{ID}}",
            "uiEditTemplate": f"https://halcyonadvisory.lightning.force.com/{{ID}}/e",
            "uiNewRecord": f"https://halcyonadvisory.lightning.force.com/{self.prefix}/e",
        }

    def global_entry(self, version: str, *, can_create: bool = True, can_update: bool = True,
                     can_delete: bool = True) -> dict[str, Any]:
        urls = self.urls(version)
        return {
            "activateable": False,
            "associateEntityType": None,
            "associateParentEntity": None,
            "createable": self.createable and can_create,
            "custom": self.custom,
            "customSetting": False,
            "deepCloneable": self.name in ("Opportunity", "Campaign"),
            "deletable": self.deletable and can_delete,
            "deprecatedAndHidden": False,
            "feedEnabled": self.feed,
            "hasSubtypes": False,
            "isInterface": False,
            "isSubtype": False,
            "keyPrefix": self.prefix or None,
            "label": self.label,
            "labelPlural": self.plural,
            "layoutable": self.queryable and self.createable,
            "mergeable": self.mergeable,
            "mruEnabled": self.createable,
            "name": self.name,
            "queryable": self.queryable,
            "replicateable": self.queryable and self.name not in ("Organization",),
            "retrieveable": True,
            "searchable": self.searchable,
            "triggerable": self.createable,
            "undeletable": self.deletable,
            "updateable": self.updateable and can_update,
            "urls": {"sobject": urls["sobject"], "describe": urls["describe"], "rowTemplate": urls["rowTemplate"]},
        }


@dataclass
class Schema:
    objects: dict[str, SObject] = field(default_factory=dict)
    _lower: dict[str, SObject] = field(default_factory=dict, repr=False)
    _prefix: dict[str, SObject] = field(default_factory=dict, repr=False)
    _children: dict[str, list[ChildRel]] = field(default_factory=dict, repr=False)

    def add(self, obj: SObject) -> SObject:
        self.objects[obj.name] = obj
        self._lower[obj.name.lower()] = obj
        if obj.prefix:
            self._prefix[obj.prefix] = obj
        self._children.clear()
        return obj

    def get(self, name: str) -> SObject | None:
        return self._lower.get(name.lower())

    def by_prefix(self, prefix: str) -> SObject | None:
        return self._prefix.get(prefix)

    def for_id(self, record_id: str) -> SObject | None:
        return self._prefix.get(record_id[:3])

    def children(self, parent: str) -> list[ChildRel]:
        if not self._children:
            for obj in self.objects.values():
                for f in obj.fields:
                    if not f.is_ref or f.child is None:
                        continue
                    for target in f.reference_to:
                        name = f.child.get(target) if isinstance(f.child, dict) else f.child
                        if name:
                            self._children.setdefault(target, []).append(ChildRel(target, name, obj.name, f.name))
        return self._children.get(parent, [])

    def child_rel(self, parent: str, name: str) -> ChildRel | None:
        lowered = name.lower()
        return next((c for c in self.children(parent) if c.name.lower() == lowered), None)

    def describe_object(self, obj: SObject, version: str, *, can_create: bool = True,
                        can_update: bool = True, can_delete: bool = True) -> dict[str, Any]:
        entry = obj.global_entry(version, can_create=can_create, can_update=can_update, can_delete=can_delete)
        urls = obj.urls(version)
        plural_lower = obj.plural.lower()
        scopes = [{"label": f"All {plural_lower}", "name": "everything"}]
        if obj.has_owner:
            scopes.insert(0, {"label": f"My {plural_lower}", "name": "mine"})
        return {
            "actionOverrides": [],
            **{k: v for k, v in entry.items() if k != "urls"},
            "childRelationships": [
                {
                    "cascadeDelete": any(
                        (f.cascade for f in self.objects[c.child].fields if f.name == c.field)
                    ),
                    "childSObject": c.child,
                    "deprecatedAndHidden": False,
                    "field": c.field,
                    "junctionIdListNames": [],
                    "junctionReferenceTo": [],
                    "relationshipName": c.name,
                    "restrictedDelete": False,
                }
                for c in self.children(obj.name)
            ],
            "compactLayoutable": obj.createable,
            "defaultImplementation": None,
            "extendedBy": None,
            "extendsInterfaces": None,
            "fields": [f.describe(can_create=can_create, can_update=can_update) for f in obj.fields],
            "implementedBy": None,
            "implementsInterfaces": None,
            "listviewable": None,
            "lookupLayoutable": None,
            "namedLayoutInfos": [],
            "networkScopeFieldName": None,
            "recordTypeInfos": [
                {
                    "active": True,
                    "available": True,
                    "defaultRecordTypeMapping": True,
                    "developerName": "Master",
                    "master": True,
                    "name": "Master",
                    "recordTypeId": MASTER_RECORD_TYPE_ID,
                    "urls": {"layout": f"{urls['sobject']}/describe/layouts/{MASTER_RECORD_TYPE_ID}"},
                }
            ]
            if obj.createable
            else [],
            "searchLayoutable": obj.searchable,
            "supportedScopes": scopes,
            "urls": urls,
        }


SCHEMA = Schema()


def _register_all() -> None:
    from emulators.providers.salesforce import schema_crm, schema_org

    for module in (schema_org, schema_crm):
        for obj in module.OBJECTS:
            SCHEMA.add(obj)


_register_all()
