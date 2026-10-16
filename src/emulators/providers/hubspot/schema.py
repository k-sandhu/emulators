"""Property, pipeline, owner, and portal metadata endpoints."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.provider import ProviderContext, external_base

from .common import PORTAL_ID, body, fail, flag, integer, object_type, require, scopes_for, stamp
from .definitions import property_shape


def schema_scopes(kind: str, mode: str = "read") -> list[str]:
    return (
        [f"crm.schemas.{kind}.{mode}", *scopes_for(kind, mode)]
        if mode == "read"
        else [f"crm.schemas.{kind}.write"]
    )


def clean(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not k.startswith("_")}


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get("/crm/v3/properties/{objectType}/groups")
    async def groups(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind))
        archived = flag(request.query_params.get("archived"))
        return {"results": [r for r in ctx.store[f"groups:{kind}"] if r["archived"] == archived]}

    @app.get("/crm/v3/properties/{objectType}/groups/{group_name}")
    async def get_group(request: Request, objectType: str, group_name: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind))
        record = ctx.store[f"groups:{kind}"].get(group_name)
        if record is None or record["archived"] != flag(request.query_params.get("archived")):
            fail(404, "Property group not found.")
        return record

    @app.post("/crm/v3/properties/{objectType}/groups", status_code=201)
    async def create_group(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind, "write"))
        payload = await body(request)
        if (
            not isinstance(payload.get("name"), str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*", payload["name"])
            or not isinstance(payload.get("label"), str)
            or not payload["label"]
        ):
            fail(400, "A valid group name and label are required.")
        if ctx.store[f"groups:{kind}"].get(payload["name"]):
            fail(409, "A property group with this name already exists.")
        record = {
            "name": payload["name"],
            "label": payload["label"],
            "displayOrder": payload.get("displayOrder", -1),
            "archived": False,
        }
        return ctx.store[f"groups:{kind}"].put(record, key="name")

    @app.get("/crm/v3/properties/{objectType}")
    async def properties(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind))
        archived = flag(request.query_params.get("archived"))
        return {"results": [r for r in ctx.store[f"properties:{kind}"] if r["archived"] == archived]}

    @app.post("/crm/v3/properties/{objectType}", status_code=201)
    async def create_property(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind, "write"))
        payload = await body(request)
        required = ("name", "label", "type", "fieldType", "groupName")
        if any(not isinstance(payload.get(k), str) or not payload[k] for k in required):
            fail(400, "name, label, type, fieldType and groupName are required.")
        name = payload["name"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name.startswith("hs_"):
            fail(400, "Invalid custom property name.")
        if ctx.store[f"properties:{kind}"].get(name):
            fail(409, "A property with this name already exists.")
        allowed = {
            "string": {"text", "textarea", "phonenumber", "html", "file"},
            "number": {"number"},
            "enumeration": {"select", "radio", "checkbox"},
            "bool": {"booleancheckbox"},
            "date": {"date"},
            "datetime": {"date"},
        }
        if payload["fieldType"] not in allowed.get(payload["type"], set()):
            fail(400, "Property type and fieldType are incompatible.")
        if ctx.store[f"groups:{kind}"].get(payload["groupName"]) is None:
            fail(400, "Property group does not exist.")
        options = payload.get("options", [])
        if (
            not isinstance(options, list)
            or any(
                not isinstance(o, dict)
                or not isinstance(o.get("label"), str)
                or not isinstance(o.get("value"), str)
                for o in options
            )
            or len({o["value"] for o in options if isinstance(o, dict)}) != len(options)
        ):
            fail(400, "Invalid property options.")
        if payload["type"] == "enumeration" and not options:
            fail(400, "Enumeration properties require options.")
        if "hasUniqueValue" in payload and not isinstance(payload["hasUniqueValue"], bool):
            fail(400, "hasUniqueValue must be a boolean.")
        record = property_shape(name, kind, payload["type"], custom=True)
        record.update(
            {
                k: payload[k]
                for k in (
                    "label",
                    "fieldType",
                    "groupName",
                    "description",
                    "hasUniqueValue",
                    "hidden",
                    "formField",
                    "displayOrder",
                )
                if k in payload
            }
        )
        record["options"] = [{"displayOrder": i, "hidden": False, **o} for i, o in enumerate(options)]
        record.update(createdAt=stamp(ctx.clock.now()), updatedAt=stamp(ctx.clock.now()))
        return ctx.store[f"properties:{kind}"].put(record, key="name")

    @app.get("/crm/v3/properties/{objectType}/{property_name}")
    async def get_property(request: Request, objectType: str, property_name: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind))
        record = ctx.store[f"properties:{kind}"].get(property_name)
        if record is None or record["archived"] != flag(request.query_params.get("archived")):
            fail(404, "Property not found.")
        return record

    @app.patch("/crm/v3/properties/{objectType}/groups/{group_name}")
    async def update_group(request: Request, objectType: str, group_name: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind, "write"))
        record = ctx.store[f"groups:{kind}"].get(group_name)
        if record is None or record["archived"]:
            fail(404, "Property group not found.")
        payload = await body(request)
        if "label" in payload and (not isinstance(payload["label"], str) or not payload["label"]):
            fail(400, "label must be a non-empty string.")
        if "displayOrder" in payload and (
            isinstance(payload["displayOrder"], bool) or not isinstance(payload["displayOrder"], int)
        ):
            fail(400, "displayOrder must be an integer.")
        record.update({k: payload[k] for k in ("label", "displayOrder") if k in payload})
        return record

    @app.delete("/crm/v3/properties/{objectType}/groups/{group_name}", status_code=204)
    async def archive_group(request: Request, objectType: str, group_name: str) -> Response:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind, "write"))
        record = ctx.store[f"groups:{kind}"].get(group_name)
        if record is None or record["archived"]:
            fail(404, "Property group not found.")
        record["archived"] = True
        return Response(status_code=204)

    @app.patch("/crm/v3/properties/{objectType}/{property_name}")
    async def update_property(request: Request, objectType: str, property_name: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind, "write"))
        record = ctx.store[f"properties:{kind}"].get(property_name)
        if record is None or record["archived"]:
            fail(404, "Property not found.")
        payload = await body(request)
        for key in ("name", "type", "fieldType"):
            if key in payload and payload[key] != record[key]:
                fail(400, f"Property {key} cannot be changed.")
        if "groupName" in payload and ctx.store[f"groups:{kind}"].get(payload["groupName"]) is None:
            fail(400, "Property group does not exist.")
        if "label" in payload and (not isinstance(payload["label"], str) or not payload["label"]):
            fail(400, "label must be a non-empty string.")
        for key in ("hasUniqueValue", "hidden", "formField"):
            if key in payload and not isinstance(payload[key], bool):
                fail(400, f"{key} must be a boolean.")
        if "displayOrder" in payload and (
            isinstance(payload["displayOrder"], bool) or not isinstance(payload["displayOrder"], int)
        ):
            fail(400, "displayOrder must be an integer.")
        if "options" in payload:
            options = payload["options"]
            if (
                not isinstance(options, list)
                or any(
                    not isinstance(o, dict)
                    or not isinstance(o.get("label"), str)
                    or not isinstance(o.get("value"), str)
                    for o in options
                )
                or len({o["value"] for o in options if isinstance(o, dict)}) != len(options)
                or (record["type"] == "enumeration" and not options)
            ):
                fail(400, "Invalid property options.")
            payload = {
                **payload,
                "options": [
                    {"displayOrder": i, "hidden": False, "description": "", **o}
                    for i, o in enumerate(options)
                ],
            }
        record.update(
            {
                k: payload[k]
                for k in (
                    "label",
                    "groupName",
                    "description",
                    "options",
                    "hasUniqueValue",
                    "hidden",
                    "formField",
                    "displayOrder",
                )
                if k in payload
            }
        )
        record["updatedAt"] = stamp(ctx.clock.now())
        return record

    @app.delete("/crm/v3/properties/{objectType}/{property_name}", status_code=204)
    async def archive_property(request: Request, objectType: str, property_name: str) -> Response:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind, "write"))
        record = ctx.store[f"properties:{kind}"].get(property_name)
        if record is None:
            fail(404, "Property not found.")
        if record["hubspotDefined"]:
            fail(400, "HubSpot-defined properties cannot be archived.")
        record["archived"] = True
        return Response(status_code=204)

    @app.get("/crm/v3/pipelines/{objectType}")
    async def pipelines(request: Request, objectType: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind))
        if kind not in ("deals", "tickets"):
            fail(400, "This object type does not support pipelines.")
        return {"results": ctx.store[f"pipelines:{kind}"].values()}

    @app.get("/crm/v3/pipelines/{objectType}/{pipeline_id}")
    async def pipeline(request: Request, objectType: str, pipeline_id: str) -> dict[str, Any]:
        kind = object_type(objectType)
        require(request, ctx, schema_scopes(kind))
        record = ctx.store[f"pipelines:{kind}"].get(pipeline_id)
        if record is None:
            fail(404, "Pipeline not found.")
        return record

    @app.get("/crm/v3/pipelines/{objectType}/{pipeline_id}/stages")
    async def stages(request: Request, objectType: str, pipeline_id: str) -> dict[str, Any]:
        return {"results": (await pipeline(request, objectType, pipeline_id))["stages"]}

    @app.get("/crm/v3/pipelines/{objectType}/{pipeline_id}/stages/{stage_id}")
    async def stage(request: Request, objectType: str, pipeline_id: str, stage_id: str) -> dict[str, Any]:
        found = next(
            (s for s in (await pipeline(request, objectType, pipeline_id))["stages"] if s["id"] == stage_id),
            None,
        )
        if found is None:
            fail(404, "Pipeline stage not found.")
        return found

    @app.get("/crm/v3/owners")
    @app.get("/crm/v3/owners/", include_in_schema=False)
    async def owners(request: Request) -> dict[str, Any]:
        require(request, ctx, ["crm.objects.owners.read"])
        q = request.query_params
        limit = integer(q.get("limit"), 100, 500)
        after = integer(q.get("after"), 0, 2**63 - 1, minimum=0)
        archived = flag(q.get("archived"))
        rows = sorted(
            [
                r
                for r in ctx.store["owners"]
                if r["archived"] == archived
                and int(r["id"]) > after
                and (not q.get("email") or r["email"].casefold() == q["email"].casefold())
                and (not q.get("userId") or str(r.get("userId")) == q["userId"])
            ],
            key=lambda r: int(r["id"]),
        )
        result: dict[str, Any] = {"results": [clean(r) for r in rows[:limit]]}
        if len(rows) > limit:
            cursor = rows[limit - 1]["id"]
            query = [(k, v) for k, v in q.multi_items() if k != "after"] + [("after", cursor)]
            result["paging"] = {
                "next": {
                    "after": cursor,
                    "link": external_base(request) + "/crm/v3/owners?" + urlencode(query),
                }
            }
        return result

    @app.get("/crm/v3/owners/{owner_id}")
    async def owner(request: Request, owner_id: str) -> dict[str, Any]:
        require(request, ctx, ["crm.objects.owners.read"])
        id_property = request.query_params.get("idProperty", "id")
        if id_property not in ("id", "userId"):
            fail(400, "idProperty must be id or userId.")
        record = next(
            (
                r
                for r in ctx.store["owners"]
                if str(r.get(id_property)) == owner_id
                and r["archived"] == flag(request.query_params.get("archived"))
            ),
            None,
        )
        if record is None:
            fail(404, "Owner not found.")
        return clean(record)

    @app.get("/account-info/v3/details")
    async def account(request: Request) -> dict[str, Any]:
        require(request, ctx, ["account-info.security.read", "oauth"])
        offset = ctx.clock.now().astimezone(ZoneInfo("America/Toronto")).utcoffset()
        offset_ms = int(offset.total_seconds() * 1000) if offset else 0
        return {
            "portalId": PORTAL_ID,
            "timeZone": "America/Toronto",
            "companyCurrency": ctx.world.org.currency,
            "uiDomain": "app.hubspot.com",
            "dataHostingLocation": "na1",
            "accountType": "STANDARD",
            "additionalCurrencies": [],
            "utcOffset": ("-" if offset_ms < 0 else "+")
            + f"{abs(offset_ms) // 3600000:02d}:{abs(offset_ms) % 3600000 // 60000:02d}",
            "utcOffsetMilliseconds": offset_ms,
        }

    @app.get("/account-info/v3/api-usage/daily/private-apps")
    async def usage(request: Request) -> dict[str, Any]:
        require(request, ctx, ["account-info.security.read"])
        return {
            "results": [
                {
                    "name": "private-apps",
                    "currentUsage": len(ctx.journal.entries(limit=5000)),
                    "usageLimit": 1000000,
                    "collectedAt": stamp(ctx.clock.now()),
                    "fetchStatus": "SUCCESS",
                    "resetsAt": stamp(
                        ctx.clock.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                    ),
                }
            ]
        }
