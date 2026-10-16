"""Group writes share the directory's owner/admin authorization and change stream."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.provider import ProviderContext
from emulators.providers.microsoft import common as c
from emulators.providers.microsoft import directory_support as s


def references(ctx: ProviderContext, value: Any, *, owners: bool = False) -> list[str]:
    from emulators.providers.microsoft.directory import object_record

    if not isinstance(value, list) or not all(isinstance(ref, str) for ref in value):
        raise c.invalid_json()
    result = []
    for ref in value:
        try:
            parsed = urlsplit(ref)
        except ValueError:
            raise c.graph_error(400, "Request_BadRequest", "Invalid object reference.") from None
        if parsed.hostname != "graph.microsoft.com":
            raise c.graph_error(400, "Request_BadRequest", "Invalid object reference.")
        identifier = parsed.path.rsplit("/", 1)[-1]
        obj = object_record(ctx, identifier)
        if owners and obj["@odata.type"] != "#microsoft.graph.user":
            raise c.graph_error(400, "Request_BadRequest", "Group owners must be users.")
        if identifier not in result:
            result.append(identifier)
    return result


def validate(payload: dict[str, Any], *, create: bool = False) -> None:
    if create and not all(
        k in payload for k in ("displayName", "mailEnabled", "mailNickname", "securityEnabled")
    ):
        raise c.graph_error(400, "Request_BadRequest", "Required group properties are missing.")
    for key in ("displayName", "mailNickname", "description", "visibility"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            raise c.invalid_json()
    for key in ("mailEnabled", "securityEnabled"):
        if key in payload and not isinstance(payload[key], bool):
            raise c.invalid_json()
    types = payload.get("groupTypes", [])
    if not isinstance(types, list) or any(t not in ("Unified", "DynamicMembership") for t in types):
        raise c.graph_error(400, "Request_BadRequest", "Invalid groupTypes.")
    if create and (not payload["displayName"] or not payload["mailNickname"]):
        raise c.graph_error(400, "Request_BadRequest", "displayName and mailNickname cannot be empty.")
    if create and not payload["mailEnabled"] and not payload["securityEnabled"]:
        raise c.graph_error(400, "Request_BadRequest", "The group must be mail-enabled or security-enabled.")
    if "Unified" in types and not payload.get("mailEnabled", True):
        raise c.graph_error(400, "Request_BadRequest", "Microsoft 365 groups must be mail-enabled.")


def register(router: APIRouter, ctx: ProviderContext) -> None:
    @router.post("/groups")
    async def create(request: Request) -> Response:
        s.admin(request, ctx)
        payload = await c.json_body(request)
        validate(payload, create=True)
        members = references(ctx, payload.get("members@odata.bind", []))
        owners = references(ctx, payload.get("owners@odata.bind", []), owners=True)
        record = {
            "id": ctx.ids.guid(),
            "createdDateTime": s.now(ctx),
            "deletedDateTime": None,
            "description": None,
            "classification": None,
            "groupTypes": [],
            "visibility": None,
            "mail": None,
            "renewedDateTime": s.now(ctx),
            "resourceProvisioningOptions": [],
            "resourceBehaviorOptions": [],
            "proxyAddresses": [],
            "onPremisesSyncEnabled": None,
            "preferredLanguage": None,
            "membershipRule": None,
            "membershipRuleProcessingState": None,
            **{
                k: copy.deepcopy(v)
                for k, v in payload.items()
                if not k.startswith(("@", "_")) and "@odata.bind" not in k and k != "id"
            },
        }
        if record["mailEnabled"]:
            record["mail"] = f"{record['mailNickname']}@{ctx.world.org.domain}"
            record["proxyAddresses"] = ["SMTP:" + record["mail"]]
        if any(
            g.get("mailNickname", "").casefold() == record["mailNickname"].casefold()
            for g in ctx.store["groups"]
        ):
            raise c.graph_error(400, "Request_BadRequest", "Another group with this mailNickname exists.")
        ctx.store["groups"].put(record)
        ctx.store.meta["ms_group_members"][record["id"]] = members
        ctx.store.meta["ms_group_owners"][record["id"]] = owners
        await c.publish_change(ctx, f"groups/{record['id']}", "created", record)
        return JSONResponse(s.single(request, record, "groups/$entity"), status_code=201)

    async def mutate(request: Request, gid: str) -> Response:
        caller = c.principal(request, ctx)
        record = ctx.store["groups"].get(gid)
        if record is None:
            raise c.not_found(gid)
        if caller.user_id not in ctx.store.meta["ms_group_owners"].get(gid, []):
            s.admin(request, ctx)
        if request.method == "DELETE":
            ctx.store["groups"].delete(gid)
            ctx.store.meta["ms_group_members"].pop(gid, None)
            ctx.store.meta["ms_group_owners"].pop(gid, None)
            for members in ctx.store.meta["ms_group_members"].values():
                if gid in members:
                    members.remove(gid)
            await c.publish_change(ctx, f"groups/{gid}", "deleted", record)
        else:
            payload = await c.json_body(request)
            validate(payload)
            allowed = {
                "displayName",
                "description",
                "mailNickname",
                "visibility",
                "preferredLanguage",
                "classification",
                "membershipRule",
                "membershipRuleProcessingState",
            }
            if set(payload) - allowed:
                raise c.graph_error(400, "Request_BadRequest", "One or more properties cannot be updated.")
            record.update(copy.deepcopy(payload))
            await c.publish_change(ctx, f"groups/{gid}", "updated", record)
        return Response(status_code=204)

    router.add_api_route("/groups/{gid}", mutate, methods=["PATCH", "DELETE"])
