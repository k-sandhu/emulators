"""SOQL execution over the in-memory org, with sharing enforced for the calling user.

``QueryEngine.execute(text)`` parses with :mod:`soql_parser` and returns the full result
(already in REST wire shape); the REST layer slices it into batches and cursors. Invisible
records are silently filtered, never errors, exactly like Salesforce.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from emulators.providers.salesforce import soql_dates
from emulators.providers.salesforce.common import (
    SfError,
    fmt_dt,
    norm_id,
    parse_dt,
)
from emulators.providers.salesforce.org import Org, Record
from emulators.providers.salesforce.schema import SCHEMA, Field, SObject
from emulators.providers.salesforce.sharing import Access
from emulators.providers.salesforce.soql_parser import (
    AGGREGATES,
    DATE_FUNCTIONS,
    INVALID_FIELD_HINT,
    INVALID_TYPE_HINT,
    Cmp,
    Func,
    Lit,
    Logic,
    Not,
    OrderItem,
    Query,
    Ref,
    SubQuery,
    Token,
    TypeOf,
    parse,
    query_error,
)

MAX_OFFSET = 2000
MAX_RELATIONSHIP_DEPTH = 5


@dataclass
class Result:
    records: list[dict[str, Any]]
    total: int
    count_only: bool = False
    entity: str = ""
    viewed: list[tuple[SObject, Record]] = field(default_factory=list)


@dataclass
class Hop:
    field: Field
    targets: list[SObject]

    @property
    def rel_name(self) -> str:
        return self.field.relationship or self.field.name


TYPE_FIELD = Field("Type", "string", label="Type")


def poly_type(obj: SObject, rec: Record) -> str:
    """``Owner.Type`` / ``Who.Type`` / ``What.Type``: the concrete sObject behind a polymorphic reference."""
    if obj.name == "Group" and rec.get("Type") == "Queue":
        return "Queue"
    return obj.name


@dataclass
class PathPlan:
    hops: list[Hop]
    name: str  # canonical final field name (or requested name for polymorphic targets)
    field: Field | None  # static field when it resolves on every target
    targets: list[SObject]  # objects the final field lives on
    ref: Ref
    poly: bool = (
        False  # the last hop is polymorphic (Who/What/Owner): ``Name`` maps to each target's name field
    )

    @property
    def key(self) -> str:
        return ".".join([h.field.name.lower() for h in self.hops] + [self.name.lower()])


@dataclass
class Group:
    records: list[Record]
    keys: tuple[Any, ...] = ()
    rolled: frozenset[int] = (
        frozenset()
    )  # indexes of GROUP BY ROLLUP/CUBE keys aggregated across (GROUPING() = 1)


class QueryEngine:
    def __init__(
        self,
        org: Org,
        access: Access,
        version: str = "62.0",
        *,
        now: datetime | None = None,
    ) -> None:
        self.org = org
        self.access = access
        self.version = version
        self.now = now or org.now()
        self.tz: str | None = access.user.get("TimeZoneSidKey")
        self.fiscal_start = int(
            (org.get("Organization", org.org_id18) or {}).get("FiscalYearStartMonth") or 1
        )
        self._readable: dict[str, Callable[[Record], bool]] = {}
        self._indexes: dict[tuple[str, str], dict[str, list[Record]]] = {}
        self._text = ""
        self._key_idx: dict[str, int] = {}
        self._from_fields: set[int] = set()  # Ref objects produced by FIELDS(): silently de-duplicated
        self.viewed: list[tuple[SObject, Record]] = []

    # -- errors ------------------------------------------------------------------------
    def err(self, tok: Token | None, message: str, code: str = "MALFORMED_QUERY") -> SfError:
        return query_error(self._text, tok, message, code)

    def invalid_field(self, tok: Token | None, name: str, entity: str) -> SfError:
        return self.err(
            tok, f"No such column '{name}' on entity '{entity}'. {INVALID_FIELD_HINT}", "INVALID_FIELD"
        )

    # -- entry points ------------------------------------------------------------------
    def execute(self, text: str, *, all_rows: bool = False) -> Result:
        self._text = text
        query = parse(text)
        return self.run(query, all_rows=all_rows)

    def run(self, query: Query, *, all_rows: bool = False) -> Result:
        self._text = query.text
        obj = SCHEMA.get(query.from_name)
        if obj is None or not obj.queryable:
            raise self.err(
                query.from_tok,
                f"sObject type '{query.from_name}' is not supported. {INVALID_TYPE_HINT}",
                "INVALID_TYPE",
            )
        if query.offset is not None and query.offset > MAX_OFFSET:
            raise self.err(query.from_tok, "Maximum SOQL offset allowed is 2000")
        if obj.name == "ContentDocumentLink" and not self._link_filter_ok(query.where):
            raise self.err(
                query.from_tok,
                "Implementation restriction: ContentDocumentLink requires a filter by a single Id on "
                "ContentDocumentId or LinkedEntityId using the equals operator or multiple Id's using the IN operator.",
            )
        include_deleted = all_rows or query.all_rows
        has_aggregate = any(self._is_aggregate(i) for i in query.select) or bool(query.group_by)
        if has_aggregate:
            return self._run_aggregate(query, obj, include_deleted)
        return self._run_plain(query, obj, include_deleted)

    def _link_filter_ok(self, cond: Any) -> bool:
        if isinstance(cond, Cmp):
            return (
                isinstance(cond.left, Ref)
                and cond.left.parts[-1].lower() in ("linkedentityid", "contentdocumentid")
                and cond.op in ("=", "IN")
            )
        return (
            isinstance(cond, Logic) and cond.op == "AND" and any(self._link_filter_ok(c) for c in cond.items)
        )

    # -- sources -----------------------------------------------------------------------
    def readable(self, obj: SObject) -> Callable[[Record], bool]:
        pred = self._readable.get(obj.name)
        if pred is None:
            pred = self._readable[obj.name] = self.access.readable(obj)
        return pred

    def source(self, obj: SObject, query: Query | None, include_deleted: bool) -> list[Record]:
        pred = self.readable(obj)
        mine = bool(query and query.scope == "mine" and obj.has_owner)
        uid = self.access.user["Id"]
        out = []
        for rec in self.org.coll(obj.name):
            if rec.get("_Purged"):
                continue
            if rec.get("IsDeleted") and not include_deleted:
                continue
            if mine and rec.get("OwnerId") != uid:
                continue
            if pred(rec):
                out.append(rec)
        return out

    # -- path resolution ---------------------------------------------------------------
    def resolve_ref(self, obj: SObject, ref: Ref, alias: str | None) -> PathPlan:
        parts = list(ref.parts)
        if alias and len(parts) > 1 and parts[0].lower() == alias.lower():
            parts = parts[1:]
        if len(parts) - 1 > MAX_RELATIONSHIP_DEPTH:
            raise self.err(ref.tok, "Relationship depth exceeds the maximum of 5")
        cur = [obj]
        hops: list[Hop] = []
        for part in parts[:-1]:
            rel_field = next((f for o in cur if (f := o.rel(part)) is not None), None)
            if rel_field is None:
                raise self.err(
                    ref.tok,
                    f"Didn't understand relationship '{part}' in field path. If you are attempting to use a "
                    "custom relationship, be sure to append the '__r' after the custom relationship name. "
                    "Please reference your WSDL or the describe call for the appropriate names.",
                    "INVALID_FIELD",
                )
            targets = [SCHEMA.objects[t] for t in rel_field.reference_to if t in SCHEMA.objects]
            hops.append(Hop(rel_field, targets))
            cur = targets
        last = parts[-1]
        poly = bool(hops) and hops[-1].field.polymorphic
        if poly and last.lower() == "type":
            return PathPlan(hops=hops, name="Type", field=TYPE_FIELD, targets=cur, ref=ref, poly=True)
        found = [(o, f) for o in cur if (f := self._field_on(o, last, poly)) is not None]
        if not found:
            raise self.invalid_field(ref.tok, last, cur[0].name if cur else obj.name)
        direct = next((f for o in cur if (f := o.field(last)) is not None), None)
        return PathPlan(
            hops=hops,
            name=direct.name if direct is not None else "Name",
            field=found[0][1],
            targets=[o for o, _ in found],
            ref=ref,
            poly=poly,
        )

    @staticmethod
    def _field_on(obj: SObject, name: str, poly: bool = False) -> Field | None:
        f = obj.field(name)
        if f is None and poly and name.lower() == "name" and obj.name_field != "Name":
            return obj.field(obj.name_field)
        return f

    def walk(self, plan: PathPlan, rec: Record) -> tuple[SObject, Record] | None:
        """Follow the relationship hops; ``None`` when any parent is missing or invisible."""
        cur_rec = rec
        cur_obj: SObject | None = None
        for hop in plan.hops:
            rid = cur_rec.get(hop.field.name)
            if not rid:
                return None
            found = self.org.find(rid)
            if found is None:
                return None
            cur_obj, cur_rec = found
            if cur_obj.name not in hop.field.reference_to or cur_rec.get("IsDeleted"):
                return None
            if not self.readable(cur_obj)(cur_rec):
                return None
        return (cur_obj, cur_rec) if cur_obj else None

    def value_of(self, plan: PathPlan, rec: Record, root: SObject) -> Any:
        if plan.hops:
            hit = self.walk(plan, rec)
            if hit is None:
                return None
            obj, target = hit
        else:
            obj, target = root, rec
        if plan.field is TYPE_FIELD:
            return poly_type(obj, target)
        f = self._field_on(obj, plan.name, plan.poly)
        if f is None:
            return None
        return self.org.field_value(obj, target, f.name, self.version)

    # -- plain queries -----------------------------------------------------------------
    def _is_aggregate(self, item: Any) -> bool:
        if isinstance(item, Func) and item.name == "FORMAT" and item.args:
            return self._is_aggregate(item.args[0])
        return isinstance(item, Func) and item.name in AGGREGATES

    def _expand_select(self, query: Query, obj: SObject) -> list[Any]:
        items: list[Any] = []
        for item in query.select:
            if isinstance(item, Func) and item.name == "FIELDS":
                kind = (item.args[0].dotted if item.args and isinstance(item.args[0], Ref) else "").upper()
                if kind not in ("ALL", "STANDARD", "CUSTOM"):
                    raise self.err(item.tok, f"Unknown FIELDS argument '{kind}'")
                if kind in ("ALL", "CUSTOM") and (query.limit is None or query.limit > 200):
                    raise self.err(item.tok, "The SOQL FIELDS function must have a LIMIT of at most 200")
                chosen = {
                    "ALL": obj.fields,
                    "STANDARD": obj.standard_fields,
                    "CUSTOM": obj.custom_fields,
                }[kind]
                expanded = [Ref([f.name], item.tok) for f in chosen]
                self._from_fields.update(id(r) for r in expanded)
                items.extend(expanded)
            else:
                items.append(item)
        return items

    def _run_plain(self, query: Query, obj: SObject, include_deleted: bool) -> Result:
        select = self._expand_select(query, obj)
        compiled = self._compile_select(query, obj, select)
        where = self.compile_cond(query.where, obj, query.alias) if query.where else None
        rows = self.source(obj, query, include_deleted)
        if where:
            rows = [r for r in rows if where(r)]
        rows = self._order(query, obj, rows)
        if query.offset:
            rows = rows[query.offset :]
        if query.limit is not None:
            rows = rows[: query.limit]
        out = [self._project(compiled, obj, rec) for rec in rows]
        result = Result(out, len(out), entity=obj.name)
        if {"VIEW", "REFERENCE"} & set(query.for_clauses):
            result.viewed = [(obj, r) for r in rows]
        return result

    def _is_count_only(self, select: list[Any]) -> bool:
        return (
            len(select) == 1 and isinstance(select[0], Func) and select[0].name == "COUNT" and select[0].star
        )

    def _compile_select(self, query: Query, obj: SObject, select: list[Any]) -> list[tuple[str, Any]]:
        compiled: list[tuple[str, Any]] = []
        seen: set[str] = set()
        exprs = 0
        for item in select:
            if isinstance(item, Ref):
                plan = self.resolve_ref(obj, item, query.alias)
                if plan.key in seen:
                    if id(item) in self._from_fields:
                        continue
                    raise self.err(item.tok, f"duplicate field selected: {plan.name}")
                seen.add(plan.key)
                compiled.append(("ref", plan))
            elif isinstance(item, SubQuery):
                compiled.append(("sub", self._compile_subquery(item, obj)))
            elif isinstance(item, TypeOf):
                compiled.append(("typeof", self._compile_typeof(item, obj, query.alias)))
            elif isinstance(item, Func):
                if item.name in ("TOLABEL", "FORMAT", "CONVERTCURRENCY") and item.args:
                    arg = item.args[0]
                    if not isinstance(arg, Ref):
                        raise self.err(item.tok, f"unexpected token: '{item.name}'")
                    plan = self.resolve_ref(obj, arg, query.alias)
                    if item.name == "FORMAT":
                        key = item.alias or f"expr{exprs}"
                        exprs += 0 if item.alias else 1
                        compiled.append(("format", (key, plan)))
                    else:
                        compiled.append(("ref", plan))
                elif item.name in DATE_FUNCTIONS or item.name == "CONVERTTIMEZONE":
                    raise self.err(item.tok, f"{item.name}() is only allowed in aggregate queries (GROUP BY)")
                elif item.name in AGGREGATES:
                    raise self.err(item.tok, "Aggregate functions require GROUP BY or must be the only field")
                else:
                    raise self.err(item.tok, f"Unknown function '{item.name}'", "INVALID_FIELD")
        return compiled

    def _project(self, compiled: list[tuple[str, Any]], obj: SObject, rec: Record) -> dict[str, Any]:
        row: dict[str, Any] = {"attributes": self.org.attributes(obj.name, rec["Id"], self.version)}
        for kind, item in compiled:
            if kind == "ref":
                self._put_path(row, item, obj, rec)
            elif kind == "format":
                key, plan = item
                row[key] = self._format(self.value_of(plan, rec, obj), plan.field)
            elif kind == "sub":
                row[item["name"]] = self._run_child(item, rec)
            elif kind == "typeof":
                self._put_typeof(row, item, obj, rec)
        return row

    def _format(self, value: Any, f: Field | None) -> Any:
        """SOQL FORMAT(): locale-formatted text (en_US: ``$35,500.00``, ``10/16/2026``)."""
        if value is None or f is None:
            return value
        if f.type == "currency":
            return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"
        if f.type == "percent":
            return f"{value:g}%"
        if f.type == "int":
            return f"{value:,}"
        if f.type == "double":
            return f"{value:,.{f.scale}f}"
        if f.type == "date":
            y, m, d = value.split("-")
            return f"{int(m)}/{int(d)}/{y}"
        if f.type == "datetime":
            dt = parse_dt(value)
            if dt is None:
                return value
            local = dt.astimezone(soql_dates.zone(self.tz))
            hour = local.hour % 12 or 12
            return f"{local.month}/{local.day}/{local.year} {hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
        return value

    def _put_path(self, row: dict[str, Any], plan: PathPlan, root: SObject, rec: Record) -> None:
        node = row
        cur_rec = rec
        cur_obj = root
        for hop in plan.hops:
            existing = node.get(hop.rel_name, False)
            if existing is None:
                return
            rid = cur_rec.get(hop.field.name)
            found = self.org.find(rid) if rid else None
            if (
                found is None
                or found[0].name not in hop.field.reference_to
                or found[1].get("IsDeleted")
                or not self.readable(found[0])(found[1])
            ):
                node[hop.rel_name] = None
                return
            cur_obj, cur_rec = found
            if existing is False:
                attrs = self.org.attributes(cur_obj.name, cur_rec["Id"], self.version)
                if hop.field.polymorphic:
                    attrs = {**attrs, "type": "Name"}  # Who / What / Owner come back typed as ``Name``
                node[hop.rel_name] = {"attributes": attrs}
            node = node[hop.rel_name]
        if plan.field is TYPE_FIELD:
            node["Type"] = poly_type(cur_obj, cur_rec)
            return
        f = self._field_on(cur_obj, plan.name, plan.poly)
        if f is not None:
            node[plan.name] = self.org.field_value(cur_obj, cur_rec, f.name, self.version)

    # -- child subqueries --------------------------------------------------------------
    def _compile_subquery(self, item: SubQuery, parent: SObject) -> dict[str, Any]:
        q = item.query
        rel = SCHEMA.child_rel(parent.name, q.from_name)
        if rel is None:
            raise self.err(
                q.from_tok,
                f"Didn't understand relationship '{q.from_name}' in FROM part of query call. If you are "
                "attempting to use a custom relationship, be sure to append the '__r' after the custom "
                "relationship name. Please reference your WSDL or the describe call for the appropriate names.",
                "INVALID_TYPE",
            )
        child = SCHEMA.objects[rel.child]
        select = self._expand_select(q, child)
        compiled = self._compile_select(q, child, select)
        where = self.compile_cond(q.where, child, q.alias) if q.where else None
        return {
            "name": rel.name,
            "child": child,
            "field": rel.field,
            "query": q,
            "compiled": compiled,
            "where": where,
        }

    def _run_child(self, spec: dict[str, Any], parent_rec: Record) -> dict[str, Any] | None:
        child: SObject = spec["child"]
        key = (child.name, spec["field"])
        index = self._indexes.get(key)
        if index is None:
            index = {}
            pred = self.readable(child)
            for rec in self.org.coll(child.name):
                if rec.get("IsDeleted") or rec.get("_Purged") or not pred(rec):
                    continue
                index.setdefault(rec.get(spec["field"]) or "", []).append(rec)
            self._indexes[key] = index
        rows = list(index.get(parent_rec["Id"], []))
        if spec["where"]:
            rows = [r for r in rows if spec["where"](r)]
        rows = self._order(spec["query"], child, rows)
        if spec["query"].offset:
            rows = rows[spec["query"].offset :]
        if spec["query"].limit is not None:
            rows = rows[: spec["query"].limit]
        if not rows:
            return None
        records = [self._project(spec["compiled"], child, r) for r in rows]
        return {"totalSize": len(records), "done": True, "records": records}

    # -- TYPEOF ------------------------------------------------------------------------
    def _compile_typeof(self, item: TypeOf, obj: SObject, alias: str | None) -> dict[str, Any]:
        rel_field = obj.rel(item.ref.parts[-1])
        if rel_field is None or not rel_field.polymorphic:
            raise self.err(
                item.tok, f"TYPEOF is only valid for polymorphic relationship fields: {item.ref.dotted}"
            )
        branches: dict[str, list[Field]] = {}
        for type_name, refs in item.whens:
            target = SCHEMA.get(type_name)
            if target is None:
                raise self.err(item.tok, f"sObject type '{type_name}' is not supported.", "INVALID_TYPE")
            fields = []
            for r in refs:
                f = target.field(r.parts[-1])
                if f is None:
                    raise self.invalid_field(r.tok, r.parts[-1], target.name)
                fields.append(f)
            branches[target.name] = fields
        else_fields = None
        if item.else_fields is not None:
            else_fields = []
            for r in item.else_fields:
                f = SCHEMA.objects[rel_field.reference_to[0]].field(r.parts[-1])
                if f is None:
                    raise self.invalid_field(r.tok, r.parts[-1], rel_field.reference_to[0])
                else_fields.append(f)
        return {"field": rel_field, "branches": branches, "else": else_fields}

    def _put_typeof(self, row: dict[str, Any], spec: dict[str, Any], root: SObject, rec: Record) -> None:
        rel_field: Field = spec["field"]
        rid = rec.get(rel_field.name)
        found = self.org.find(rid) if rid else None
        if found is None or not self.readable(found[0])(found[1]):
            row[rel_field.relationship or rel_field.name] = None
            return
        target_obj, target = found
        fields = spec["branches"].get(target_obj.name, spec["else"])
        node: dict[str, Any] = {
            "attributes": self.org.attributes(target_obj.name, target["Id"], self.version)
        }
        for f in fields or []:
            ff = target_obj.field(f.name)
            if ff is not None:
                node[ff.name] = self.org.field_value(target_obj, target, ff.name, self.version)
        row[rel_field.relationship or rel_field.name] = node

    # -- ordering ----------------------------------------------------------------------
    def _order(self, query: Query, obj: SObject, rows: list[Record]) -> list[Record]:
        if not query.order_by:
            return rows
        plans = []
        for item in query.order_by:
            if not isinstance(item.expr, Ref):
                raise self.err(query.from_tok, "ORDER BY expression is not supported here")
            plan = self.resolve_ref(obj, item.expr, query.alias)
            if plan.field is not None and not plan.field.sortable:
                raise self.err(
                    item.expr.tok,
                    f"field '{plan.field.name}' can not be sorted in a query call",
                    "INVALID_FIELD",
                )
            plans.append((plan, item))
        keyed = [(r, [self.value_of(p, r, obj) for p, _ in plans]) for r in rows]
        for idx in range(len(plans) - 1, -1, -1):
            item = plans[idx][1]
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.desc
            present = [kv for kv in keyed if kv[1][idx] is not None]
            nulls = [kv for kv in keyed if kv[1][idx] is None]
            present.sort(key=lambda kv, i=idx: _sort_key(kv[1][i]), reverse=item.desc)
            keyed = nulls + present if nulls_first else present + nulls
        return [r for r, _ in keyed]

    # -- aggregates --------------------------------------------------------------------
    def _run_aggregate(self, query: Query, obj: SObject, include_deleted: bool) -> Result:
        select = query.select
        where = self.compile_cond(query.where, obj, query.alias) if query.where else None
        records = self.source(obj, query, include_deleted)
        if where:
            records = [r for r in records if where(r)]
        if self._is_count_only(select) and not query.group_by:
            if query.having:
                raise self.err(query.from_tok, "HAVING requires aggregate fields")
            n = len(records)
            if query.offset:
                n = max(0, n - query.offset)
            if query.limit is not None:
                n = min(n, query.limit)
            return Result([], n, count_only=True, entity=obj.name)
        key_plans = [self._group_expr(g, obj, query.alias) for g in query.group_by]
        self._key_idx = {self._expr_key(k["expr"]): i for i, k in enumerate(key_plans)}
        if key_plans:
            group_list = self._build_groups(records, key_plans, query.group_modifier)
        else:
            group_list = [Group(records)]
        # SELECT items
        outputs = self._compile_aggregate_select(query, obj, key_plans)
        having = self._compile_having(query.having, obj, query.alias) if query.having else None
        rows: list[tuple[Group, dict[str, Any]]] = []
        for grp in group_list:
            if having and not having(grp):
                continue
            row: dict[str, Any] = {"attributes": {"type": "AggregateResult"}}
            for name, getter in outputs:
                row[name] = getter(grp)
            rows.append((grp, row))
        if query.order_by:
            rows = self._order_groups(query, obj, rows, key_plans)
        out = [row for _, row in rows]
        if query.offset:
            out = out[query.offset :]
        if query.limit is not None:
            out = out[: query.limit]
        return Result(out, len(out), entity=obj.name)

    def _build_groups(
        self, records: list[Record], key_plans: list[dict[str, Any]], modifier: str | None
    ) -> list[Group]:
        n = len(key_plans)
        if modifier == "ROLLUP":
            levels = [frozenset(range(k, n)) for k in range(n, -1, -1)]
        elif modifier == "CUBE":
            levels = [frozenset(i for i in range(n) if mask & (1 << i)) for mask in range(1 << n)]
            levels.sort(key=len)
        else:
            levels = [frozenset()]
        out: list[Group] = []
        for rolled in levels:
            buckets: dict[tuple, list[Record]] = {}
            for rec in records:
                key = tuple(
                    None if i in rolled else _hashable(g["get"](rec)) for i, g in enumerate(key_plans)
                )
                buckets.setdefault(key, []).append(rec)
            ordered = sorted(buckets, key=lambda k: tuple(_sort_key_nullable(v) for v in k))
            out.extend(Group(buckets[k], k, rolled) for k in ordered)
        if modifier:
            out.sort(key=lambda g: tuple((1, 0) if v is None else (0, _sort_key(v)) for v in g.keys))
        return out

    def _group_expr(self, expr: Any, obj: SObject, alias: str | None) -> dict[str, Any]:
        if isinstance(expr, Ref):
            plan = self.resolve_ref(obj, expr, alias)
            if plan.field is not None and not plan.field.groupable:
                raise self.err(
                    expr.tok, f"field '{plan.field.name}' can not be grouped in a query call", "INVALID_FIELD"
                )
            return {"get": lambda rec, p=plan: self.value_of(p, rec, obj), "plan": plan, "expr": expr}
        if isinstance(expr, Func) and expr.name in DATE_FUNCTIONS:
            inner, tz = self._date_arg(expr)
            if inner is None:
                raise self.err(expr.tok, f"unexpected token: '{expr.name}'")
            plan = self.resolve_ref(obj, inner, alias)

            def get(rec: Record, p: PathPlan = plan, name: str = expr.name, tz: str | None = tz) -> Any:
                return soql_dates.date_function(name, self.value_of(p, rec, obj), tz, self.fiscal_start)

            return {"get": get, "plan": None, "expr": expr}
        if isinstance(expr, Func) and expr.name in ("TOLABEL", "FORMAT"):
            return self._group_expr(expr.args[0], obj, alias)
        raise self.err(getattr(expr, "tok", None), "unexpected token in GROUP BY")

    def _compile_aggregate_select(
        self, query: Query, obj: SObject, key_plans: list[dict[str, Any]]
    ) -> list[tuple[str, Callable[[Group], Any]]]:
        outputs: list[tuple[str, Callable[[Group], Any]]] = []
        expr_index = 0
        key_index = {self._expr_key(k["expr"]): k for k in key_plans}
        used: set[str] = set()
        for item in query.select:
            if isinstance(item, Ref):
                k = key_index.get(self._expr_key(item))
                plan = self.resolve_ref(obj, item, query.alias)
                if k is None:
                    raise self.err(
                        item.tok, f"field '{plan.name}' must be grouped or aggregated", "MALFORMED_QUERY"
                    )
                name = plan.name
                if name in used:
                    name = f"{name}{len(used)}"
                used.add(name)
                outputs.append((name, lambda grp, i=self._key_idx[self._expr_key(item)]: grp.keys[i]))
            elif isinstance(item, Func):
                if item.name in DATE_FUNCTIONS and self._expr_key(item) in key_index:
                    name = item.alias or f"expr{expr_index}"
                    if not item.alias:
                        expr_index += 1
                    outputs.append((name, lambda grp, i=self._key_idx[self._expr_key(item)]: grp.keys[i]))
                elif item.name == "GROUPING":
                    arg = item.args[0] if item.args else None
                    idx = self._key_idx.get(self._expr_key(arg)) if arg is not None else None
                    if idx is None:
                        raise self.err(item.tok, "GROUPING() requires a field from the GROUP BY clause")
                    name = item.alias or f"expr{expr_index}"
                    if not item.alias:
                        expr_index += 1
                    outputs.append((name, lambda grp, i=idx: 1 if i in grp.rolled else 0))
                elif item.name in AGGREGATES:
                    name = item.alias or f"expr{expr_index}"
                    if not item.alias:
                        expr_index += 1
                    outputs.append((name, self._aggregate_getter(item, obj, query.alias)))
                elif item.name == "FORMAT" and item.args and self._is_aggregate(item.args[0]):
                    agg = item.args[0]
                    getter = self._aggregate_getter(agg, obj, query.alias)
                    target = agg.args[0] if agg.args else None
                    fld = (
                        self.resolve_ref(obj, target, query.alias).field
                        if isinstance(target, Ref) and not agg.name.startswith("COUNT")
                        else None
                    )
                    name = item.alias or f"expr{expr_index}"
                    if not item.alias:
                        expr_index += 1
                    outputs.append((name, lambda grp, g=getter, f=fld: self._format(g(grp), f)))
                elif item.name in ("TOLABEL", "FORMAT") and item.args:
                    inner = item.args[0]
                    k = key_index.get(self._expr_key(inner))
                    if k is None:
                        raise self.err(item.tok, "field must be grouped or aggregated")
                    outputs.append(
                        (
                            self.resolve_ref(obj, inner, query.alias).name,
                            lambda grp, i=self._key_idx[self._expr_key(inner)]: grp.keys[i],
                        )
                    )
                else:
                    raise self.err(item.tok, f"Function {item.name} must be grouped or aggregated")
            elif isinstance(item, SubQuery):
                raise self.err(item.tok, "Subqueries are not allowed in aggregate queries")
            else:
                raise self.err(getattr(item, "tok", None), "Unsupported select item in aggregate query")
        return outputs

    def _date_arg(self, func: Func) -> tuple[Ref | None, str | None]:
        """The field a date function reads, and the zone to read it in.

        Date functions work in UTC unless the field is wrapped in ``convertTimezone()`` (user's zone).
        """
        arg = func.args[0] if func.args else None
        if isinstance(arg, Ref):
            return arg, None
        if (
            isinstance(arg, Func)
            and arg.name == "CONVERTTIMEZONE"
            and len(arg.args) == 1
            and isinstance(arg.args[0], Ref)
        ):
            return arg.args[0], self.tz or "UTC"
        return None, None

    @staticmethod
    def _expr_key(expr: Any) -> str:
        if isinstance(expr, Ref):
            return "ref:" + ".".join(p.lower() for p in expr.parts)
        if isinstance(expr, Func):
            return f"func:{expr.name}:" + ",".join(QueryEngine._expr_key(a) for a in expr.args)
        return "lit"

    def _aggregate_getter(self, item: Func, obj: SObject, alias: str | None) -> Callable[[Group], Any]:
        name = item.name
        if name == "COUNT" and (item.star or not item.args):
            return lambda grp: len(grp.records)
        arg = item.args[0] if item.args else None
        if not isinstance(arg, Ref):
            raise self.err(item.tok, f"unexpected token in {name}()")
        plan = self.resolve_ref(obj, arg, alias)
        f = plan.field
        if name in ("SUM", "AVG") and (f is None or not f.is_numeric):
            raise self.err(
                item.tok,
                f"Invalid field for {name}: {plan.name}. {name} requires a numeric field.",
                "INVALID_FIELD",
            )

        def values(grp: Group) -> list[Any]:
            return [v for rec in grp.records if (v := self.value_of(plan, rec, obj)) is not None]

        if name == "COUNT":
            return lambda grp: len(values(grp))
        if name == "COUNT_DISTINCT":
            return lambda grp: len({_hashable(v) for v in values(grp)})
        if name == "SUM":

            def total(grp: Group) -> Any:
                vals = values(grp)
                if not vals:
                    return None
                s = sum(vals)
                return int(s) if f is not None and f.type == "int" else float(s)

            return total
        if name == "AVG":
            return lambda grp: (sum(v := values(grp)) / len(v)) if values(grp) else None
        if name == "MIN":
            return lambda grp: min(v, key=_sort_key) if (v := values(grp)) else None
        if name == "MAX":
            return lambda grp: max(v, key=_sort_key) if (v := values(grp)) else None
        raise self.err(item.tok, f"Unknown aggregate '{name}'")

    def _order_groups(
        self,
        query: Query,
        obj: SObject,
        rows: list[tuple[Group, dict[str, Any]]],
        key_plans: list[dict[str, Any]],
    ) -> list[tuple[Group, dict[str, Any]]]:
        getters: list[tuple[Callable[[Group], Any], OrderItem]] = []
        key_index = {self._expr_key(k["expr"]): k for k in key_plans}
        for item in query.order_by:
            expr = item.expr
            if isinstance(expr, Func) and expr.name in AGGREGATES:
                getters.append((self._aggregate_getter(expr, obj, query.alias), item))
            else:
                k = key_index.get(self._expr_key(expr))
                if k is None:
                    raise self.err(getattr(expr, "tok", None), "ORDER BY field must be grouped or aggregated")
                getters.append((lambda grp, i=self._key_idx[self._expr_key(expr)]: grp.keys[i], item))
        keyed = [(gr, [g(gr[0]) for g, _ in getters]) for gr in rows]
        for idx in range(len(getters) - 1, -1, -1):
            item = getters[idx][1]
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.desc
            present = [kv for kv in keyed if kv[1][idx] is not None]
            nulls = [kv for kv in keyed if kv[1][idx] is None]
            present.sort(key=lambda kv, i=idx: _sort_key(kv[1][i]), reverse=item.desc)
            keyed = nulls + present if nulls_first else present + nulls
        return [gr for gr, _ in keyed]

    def _compile_having(self, cond: Any, obj: SObject, alias: str | None) -> Callable[[Group], bool]:
        return self._compile(cond, obj, alias, group_mode=True)

    # -- conditions --------------------------------------------------------------------
    def compile_cond(self, cond: Any, obj: SObject, alias: str | None) -> Callable[[Record], bool]:
        return self._compile(cond, obj, alias, group_mode=False)

    def _compile(
        self, cond: Any, obj: SObject, alias: str | None, *, group_mode: bool
    ) -> Callable[[Any], bool]:
        if isinstance(cond, Logic):
            parts = [self._compile(c, obj, alias, group_mode=group_mode) for c in cond.items]
            if cond.op == "AND":
                return lambda x: all(p(x) for p in parts)
            return lambda x: any(p(x) for p in parts)
        if isinstance(cond, Not):
            inner = self._compile(cond.item, obj, alias, group_mode=group_mode)
            return lambda x: not inner(x)
        if isinstance(cond, Cmp):
            return self._compile_cmp(cond, obj, alias, group_mode)
        raise self.err(None, "Unsupported condition")

    def _left_getter(
        self, left: Any, obj: SObject, alias: str | None, group_mode: bool
    ) -> tuple[Callable[[Any], Any], str, Field | None, str]:
        """(getter, type, field, display name) for the left operand of a comparison."""
        if isinstance(left, Ref):
            plan = self.resolve_ref(obj, left, alias)
            f = plan.field
            if f is not None and not f.filterable and not group_mode:
                raise self.err(
                    left.tok, f"field '{f.name}' can not be filtered in a query call", "INVALID_FIELD"
                )
            if group_mode:
                idx = self._key_idx.get(self._expr_key(left))
                if idx is not None:
                    return (lambda grp, i=idx: grp.keys[i]), (f.type if f else "string"), f, plan.name
                return (
                    (lambda grp, p=plan: self.value_of(p, grp.records[0], obj)),
                    (f.type if f else "string"),
                    f,
                    plan.name,
                )
            return (lambda rec, p=plan: self.value_of(p, rec, obj)), (f.type if f else "string"), f, plan.name
        if isinstance(left, Func):
            if left.name == "GROUPING" and group_mode and left.args:
                idx = self._key_idx.get(self._expr_key(left.args[0]))
                if idx is None:
                    raise self.err(left.tok, "GROUPING() requires a field from the GROUP BY clause")
                return (lambda grp, i=idx: 1 if i in grp.rolled else 0), "int", None, "GROUPING"
            if left.name in AGGREGATES:
                if not group_mode:
                    raise self.err(left.tok, "Aggregate functions are only allowed in HAVING")
                getter = self._aggregate_getter(left, obj, alias)
                arg = left.args[0] if left.args else None
                ftype = "int" if left.name.startswith("COUNT") else "double"
                if arg is not None and isinstance(arg, Ref) and left.name in ("MIN", "MAX"):
                    f = self.resolve_ref(obj, arg, alias).field
                    ftype = f.type if f else ftype
                return getter, ftype, None, left.name
            if left.name in DATE_FUNCTIONS and left.args:
                inner, tz = self._date_arg(left)
                if inner is None:
                    raise self.err(left.tok, f"unexpected token: '{left.name}'")
                plan = self.resolve_ref(obj, inner, alias)
                name = left.name
                ftype = "string" if name == "DAY_ONLY" else "int"
                if group_mode:
                    return (
                        lambda grp, p=plan, n=name, z=tz: soql_dates.date_function(
                            n, self.value_of(p, grp.records[0], obj), z, self.fiscal_start
                        ),
                        ftype,
                        None,
                        name,
                    )
                return (
                    lambda rec, p=plan, n=name, z=tz: soql_dates.date_function(
                        n, self.value_of(p, rec, obj), z, self.fiscal_start
                    ),
                    ftype,
                    None,
                    name,
                )
            if left.name in ("TOLABEL", "FORMAT") and left.args and isinstance(left.args[0], Ref):
                return self._left_getter(left.args[0], obj, alias, group_mode)
            raise self.err(left.tok, f"unexpected token: '{left.name}'")
        raise self.err(getattr(left, "tok", None), "unexpected token")

    def _compile_cmp(
        self, cond: Cmp, obj: SObject, alias: str | None, group_mode: bool
    ) -> Callable[[Any], bool]:
        getter, ftype, f, name = self._left_getter(cond.left, obj, alias, group_mode)
        op = cond.op
        right = cond.right
        if op in ("IN", "NOT IN"):
            if isinstance(right, SubQuery):
                values = self._semi_join(right, f, name)
            else:
                values = [self._coerce(lit, ftype, name) for lit in right]
            norm = _normalizer(ftype)
            wanted = {_hashable(norm(v)) for v in values if v is not None}
            has_null = any(v is None for v in values)
            if op == "IN":
                return lambda x: (
                    lambda v: (v is None and has_null) or (v is not None and _hashable(norm(v)) in wanted)
                )(getter(x))
            return lambda x: (
                lambda v: (v is None and not has_null) or (v is not None and _hashable(norm(v)) not in wanted)
            )(getter(x))
        if op in ("INCLUDES", "EXCLUDES"):
            if ftype != "multipicklist":
                raise self.err(
                    cond.tok,
                    f"Field {name} is not a multipicklist; INCLUDES/EXCLUDES require one",
                    "INVALID_FIELD",
                )
            sets = [set(s.strip().lower() for s in str(lit.value).split(";") if s.strip()) for lit in right]

            def includes(x: Any) -> bool:
                raw = getter(x)
                have = {s.strip().lower() for s in str(raw).split(";")} if raw else set()
                return any(s <= have for s in sets)

            return includes if op == "INCLUDES" else (lambda x: not includes(x))
        lit = right
        if not isinstance(lit, Lit):
            raise self.err(cond.tok, "unexpected token in comparison")
        if op == "LIKE":
            if ftype not in (
                "string",
                "textarea",
                "picklist",
                "email",
                "phone",
                "url",
                "combobox",
                "id",
                "reference",
            ):
                raise self.err(
                    cond.tok, f"Field {name} can not be used with LIKE", "INVALID_QUERY_FILTER_OPERATOR"
                )
            if lit.kind != "string":
                raise self.err(
                    lit.tok,
                    f"value of filter criterion for field '{name}' must be of type string and should be enclosed in quotes",
                    "INVALID_FIELD",
                )
            regex = _like_regex(lit.value)
            return lambda x: (lambda v: v is not None and regex.fullmatch(str(v)) is not None)(getter(x))
        if lit.kind == "daterel":
            return self._compile_date_relative(cond, getter, ftype, name)
        value = self._coerce(lit, ftype, name)
        norm = _normalizer(ftype)
        if value is None:
            if op == "=":
                return lambda x: getter(x) is None
            if op == "<>":
                return lambda x: getter(x) is not None
            raise self.err(
                cond.tok, f"Invalid operator {op} for null comparison", "INVALID_QUERY_FILTER_OPERATOR"
            )
        target = norm(value)
        if ftype == "boolean" and op not in ("=", "<>"):
            raise self.err(
                cond.tok,
                f"Operator {op} is not valid for boolean field {name}",
                "INVALID_QUERY_FILTER_OPERATOR",
            )
        if op == "=":
            return lambda x: (lambda v: v is not None and norm(v) == target)(getter(x))
        if op == "<>":
            return lambda x: (lambda v: v is None or norm(v) != target)(getter(x))
        ops = {
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
        }
        fn = ops[op]
        return lambda x: (lambda v: v is not None and fn(_cmp_key(norm(v)), _cmp_key(target)))(getter(x))

    def _compile_date_relative(
        self, cond: Cmp, getter: Callable[[Any], Any], ftype: str, name: str
    ) -> Callable[[Any], bool]:
        if ftype not in ("date", "datetime"):
            raise self.err(
                cond.tok,
                f"value of filter criterion for field '{name}' must be of type {ftype} and should not be enclosed in quotes",
                "INVALID_FIELD",
            )
        lit: Lit = cond.right
        today = soql_dates.local_today(self.now, self.tz)
        start, end = soql_dates.date_range(lit.value, lit.n, today, self.fiscal_start)
        if ftype == "date":
            lo, hi = start.isoformat(), end.isoformat()
        else:
            lo, hi = soql_dates.datetime_bounds(start, end, self.tz)
        op = cond.op
        if op == "=":
            return lambda x: (lambda v: v is not None and lo <= v < hi)(getter(x))
        if op == "<>":
            return lambda x: (lambda v: v is None or not (lo <= v < hi))(getter(x))
        if op == "<":
            return lambda x: (lambda v: v is not None and v < lo)(getter(x))
        if op == "<=":
            return lambda x: (lambda v: v is not None and v < hi)(getter(x))
        if op == ">":
            return lambda x: (lambda v: v is not None and v >= hi)(getter(x))
        if op == ">=":
            return lambda x: (lambda v: v is not None and v >= lo)(getter(x))
        raise self.err(
            cond.tok, f"Operator {op} is not valid for date literals", "INVALID_QUERY_FILTER_OPERATOR"
        )

    def _semi_join(self, sub: SubQuery, f: Field | None, name: str) -> list[Any]:
        q = sub.query
        if len(q.select) != 1 or not isinstance(q.select[0], Ref):
            raise self.err(sub.tok, "Bad semi-join: the subquery must select a single field")
        child_obj = SCHEMA.get(q.from_name)
        if child_obj is None:
            raise self.err(
                q.from_tok,
                f"sObject type '{q.from_name}' is not supported. {INVALID_TYPE_HINT}",
                "INVALID_TYPE",
            )
        plan = self.resolve_ref(child_obj, q.select[0], q.alias)
        where = self.compile_cond(q.where, child_obj, q.alias) if q.where else None
        recs = self.source(child_obj, q, False)
        if where:
            recs = [r for r in recs if where(r)]
        return [v for r in recs if (v := self.value_of(plan, r, child_obj)) is not None]

    # -- literal coercion --------------------------------------------------------------
    def _coerce(self, lit: Any, ftype: str, name: str) -> Any:
        """Convert a literal (or an already-evaluated subquery value) to the field's comparison type."""
        if not isinstance(lit, Lit):
            return lit
        if lit.kind == "null":
            return None
        if ftype in ("id", "reference"):
            if lit.kind != "string":
                raise self.err(
                    lit.tok,
                    f"value of filter criterion for field '{name}' must be of type id and should be enclosed in quotes",
                    "INVALID_FIELD",
                )
            rid = norm_id(lit.value)
            if rid is None:
                raise SfError(400, "MALFORMED_ID", f"invalid ID field: {lit.value}")
            return rid
        if ftype in ("int", "double", "currency", "percent"):
            if lit.kind != "number":
                raise self.err(
                    lit.tok,
                    f"value of filter criterion for field '{name}' must be of type double and should not be enclosed in quotes",
                    "INVALID_FIELD",
                )
            return lit.value
        if ftype == "boolean":
            if lit.kind != "bool":
                raise self.err(
                    lit.tok,
                    f"value of filter criterion for field '{name}' must be of type boolean and should not be enclosed in quotes",
                    "INVALID_FIELD",
                )
            return lit.value
        if ftype == "date":
            if lit.kind == "date":
                return lit.value
            if lit.kind == "datetime":
                parsed = parse_dt(lit.value)
                return parsed.date().isoformat() if parsed else lit.value[:10]
            raise self.err(
                lit.tok,
                f"value of filter criterion for field '{name}' must be of type date and should not be enclosed in quotes",
                "INVALID_FIELD",
            )
        if ftype == "datetime":
            if lit.kind in ("date", "datetime"):
                parsed = parse_dt(lit.value)
                if parsed is None:
                    raise self.err(lit.tok, f"invalid date/time: {lit.value}", "INVALID_FIELD")
                return fmt_dt(parsed)
            raise self.err(
                lit.tok,
                f"value of filter criterion for field '{name}' must be of type dateTime and should not be enclosed in quotes",
                "INVALID_FIELD",
            )
        # strings, picklists, email...; DAY_ONLY() yields a date string compared against a date literal
        if lit.kind == "string" or (lit.kind == "date" and ftype == "string"):
            return lit.value
        raise self.err(
            lit.tok,
            f"value of filter criterion for field '{name}' must be of type string and should be enclosed in quotes",
            "INVALID_FIELD",
        )


# -- helpers ---------------------------------------------------------------------------------


def _normalizer(ftype: str) -> Callable[[Any], Any]:
    if ftype in ("string", "textarea", "picklist", "email", "phone", "url", "combobox", "multipicklist"):
        return lambda v: str(v).casefold()
    if ftype in ("int", "double", "currency", "percent"):
        return lambda v: float(v)
    return lambda v: v


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return str(value)
    return value


def _cmp_key(value: Any) -> Any:
    return value


def _sort_key(value: Any) -> Any:
    if isinstance(value, str):
        return (1, value.casefold())
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (2, str(value))


def _sort_key_nullable(value: Any) -> Any:
    return (0, 0) if value is None else (1, _sort_key(value))


def _like_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)
