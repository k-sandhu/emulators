"""The org: typed access to the records held in the provider store.

Records are kept in the wire shape (API field names as keys) in one collection per sObject,
keyed by their 18-character id. Keys starting with ``_`` are internal bookkeeping (deletion
timestamp, purge flag) and never leave the process. Counters and the Salesforce limit
counters live in ``store.meta['sf']`` so ``reset``/snapshots cover them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from emulators.core.auth import Grant
from emulators.core.ids import salesforce_id
from emulators.core.provider import ProviderContext
from emulators.core.store import Collection
from emulators.providers.salesforce.common import fmt_dt, norm_id, to_18
from emulators.providers.salesforce.schema import SCHEMA, SObject

Record = dict[str, Any]


@dataclass
class Session:
    """An authenticated REST caller: the token's grant and the user it acts as."""

    token: str
    grant: Grant
    user: Record
    version: str = "62.0"

    @property
    def user_id(self) -> str:
        return str(self.user["Id"])


class Org:
    """Record access, id minting and clock for one provider context."""

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.store = ctx.store
        self.world = ctx.world
        self.org_id15: str = ctx.world.org.salesforce_org_id
        self.org_id18: str = to_18(self.org_id15)
        self._access: dict[str, Any] = {}

    # -- bookkeeping -------------------------------------------------------------------
    @property
    def meta(self) -> dict[str, Any]:
        return self.store.meta.setdefault("sf", {"seq": {}, "api_calls": 0, "case_number": 1009})

    def now(self) -> datetime:
        return self.ctx.clock.now()

    def now_str(self) -> str:
        return fmt_dt(self.now())

    def new_id(self, obj_name: str) -> str:
        prefix = SCHEMA.objects[obj_name].prefix
        seq: dict[str, int] = self.meta["seq"]
        seq[prefix] = seq.get(prefix, 0) + 1
        return salesforce_id(prefix, seq[prefix])

    def next_case_number(self) -> str:
        self.meta["case_number"] += 1
        return f"{self.meta['case_number']:08d}"

    # -- records -----------------------------------------------------------------------
    def coll(self, obj_name: str) -> Collection:
        return self.store.collection(obj_name)

    def get(self, obj_name: str, record_id: Any) -> Record | None:
        rid = norm_id(record_id)
        if rid is None:
            return None
        return self.coll(obj_name).get(rid)

    def find(self, record_id: Any) -> tuple[SObject, Record] | None:
        """Resolve a record of any type from its key prefix."""
        rid = norm_id(record_id)
        if rid is None:
            return None
        obj = SCHEMA.for_id(rid)
        if obj is None:
            return None
        rec = self.coll(obj.name).get(rid)
        return (obj, rec) if rec is not None else None

    def put(self, obj_name: str, rec: Record) -> Record:
        return self.coll(obj_name).put_as(rec["Id"], rec)

    def live(self, obj_name: str) -> list[Record]:
        return [r for r in self.coll(obj_name) if not r.get("IsDeleted")]

    # -- users -------------------------------------------------------------------------
    def user(self, user_id: Any) -> Record | None:
        return self.get("User", user_id)

    def find_user(self, ident: str | None) -> Record | None:
        """A user by Id, world person id (``p012``), Username or Email (case-insensitive)."""
        if not ident:
            return None
        found = self.user(ident)
        if found is not None:
            return found
        lowered = ident.lower()
        for rec in self.coll("User"):
            if (
                str(rec.get("Username", "")).lower() == lowered
                or str(rec.get("Email", "")).lower() == lowered
                or str(rec.get("EmployeeNumber", "")).lower() == lowered
                or rec.get("_person_id") == ident
            ):
                return rec
        return None

    def user_name(self, user_id: Any) -> str | None:
        rec = self.user(user_id)
        return rec["Name"] if rec else None

    # -- presentation ------------------------------------------------------------------
    def attributes(self, obj_name: str, record_id: str, version: str) -> dict[str, str]:
        return {"type": obj_name, "url": f"/services/data/v{version}/sobjects/{obj_name}/{record_id}"}

    def address_value(self, obj: SObject, rec: Record, compound: str) -> dict[str, Any] | None:
        names = obj.field(compound).components if obj.field(compound) else ()  # type: ignore[union-attr]
        parts = {n: rec.get(n) for n in names}
        if all(v is None for v in parts.values()):
            return None

        def pick(suffix: str) -> Any:
            for n, v in parts.items():
                if n.endswith(suffix):
                    return v
            return None

        return {
            "city": pick("City"),
            "country": pick("Country"),
            "geocodeAccuracy": pick("GeocodeAccuracy"),
            "latitude": pick("Latitude"),
            "longitude": pick("Longitude"),
            "postalCode": pick("PostalCode"),
            "state": pick("State"),
            "street": pick("Street"),
        }

    def field_value(self, obj: SObject, rec: Record, name: str, version: str = "62.0") -> Any:
        f = obj.field(name)
        if f is not None and f.type == "address":
            return self.address_value(obj, rec, f.name)
        value = rec.get(name if f is None else f.name)
        if f is not None and f.type == "base64":
            # binary fields come back as the URL the bytes can be fetched from
            return f"/services/data/v{version}/sobjects/{obj.name}/{rec['Id']}/{f.name}" if value else None
        return value

    def render(self, obj: SObject, rec: Record, version: str, fields: list[str] | None = None) -> Record:
        """The REST representation of one record (``attributes`` first, then the fields)."""
        out: Record = {"attributes": self.attributes(obj.name, rec["Id"], version)}
        names = fields if fields is not None else [f.name for f in obj.fields]
        for name in names:
            f = obj.field(name)
            if f is None:
                continue
            out[f.name] = self.field_value(obj, rec, f.name, version)
        return out
