"""Small helpers shared by the seed modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from emulators.core.ids import stable_hash
from emulators.providers.salesforce.common import fmt_dt
from emulators.providers.salesforce.org import Org, Record
from emulators.providers.salesforce.schema import SCHEMA, SObject


def blank(obj: SObject) -> Record:
    """A record with every field present (schema order), booleans false, everything else null."""
    rec: Record = {}
    for f in obj.fields:
        if f.type == "address":
            continue
        rec[f.name] = False if f.type == "boolean" else None
    return rec


def hnum(*parts: object, mod: int = 1 << 30) -> int:
    """A deterministic pseudo-random integer derived from ``parts``."""
    return int(stable_hash(*parts, length=12), 16) % mod


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def stamp(
    rec: Record,
    created: datetime,
    *,
    modified: datetime | None = None,
    created_by: str | None = None,
    modified_by: str | None = None,
) -> Record:
    """Fill the system fields of a seeded record."""
    modified = modified or created
    for key, value in (
        ("CreatedDate", fmt_dt(created)),
        ("CreatedById", created_by),
        ("LastModifiedDate", fmt_dt(modified)),
        ("LastModifiedById", modified_by or created_by),
        ("SystemModstamp", fmt_dt(modified)),
    ):
        if key in rec:
            rec[key] = value
    return rec


def modified_after(anchor: datetime, created: datetime, *key: object, max_days: int = 30) -> datetime:
    """A last-modified instant between creation and the anchor, derived from ``key``."""
    span = max(0, int((anchor - created).total_seconds() // 3600))
    if span == 0:
        return created
    return created + timedelta(hours=hnum(*key, mod=min(span, max_days * 24) + 1))


def date_str(d: date | None) -> str | None:
    return d.isoformat() if d else None


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


@dataclass
class SeedState:
    """Cross references built while seeding (world id -> Salesforce id)."""

    org: Org
    user_by_person: dict[str, str] = field(default_factory=dict)
    account_by_client: dict[str, str] = field(default_factory=dict)
    account_by_vendor: dict[str, str] = field(default_factory=dict)
    contact_by_person: dict[str, str] = field(default_factory=dict)
    opp_by_deal: dict[str, str] = field(default_factory=dict)
    engagement_by_world: dict[str, str] = field(default_factory=dict)
    lead_by_world: dict[str, str] = field(default_factory=dict)
    campaign_ids: dict[str, str] = field(default_factory=dict)
    product_by_item: dict[str, str] = field(default_factory=dict)
    pbe_by_item: dict[str, str] = field(default_factory=dict)
    pricebook_id: str = ""
    admin_id: str = ""
    integration_id: str = ""
    converted: dict[str, dict[str, str]] = field(default_factory=dict)

    def new(self, obj_name: str, **values: Any) -> Record:
        """A blank record for ``obj_name`` with a fresh id and the given values applied."""
        obj = SCHEMA.objects[obj_name]
        rec = blank(obj)
        rec["Id"] = self.org.new_id(obj_name)
        rec.update(values)
        return rec

    def save(self, obj_name: str, rec: Record) -> Record:
        return self.org.put(obj_name, rec)
